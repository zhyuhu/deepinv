from functools import partial
import math

from dotmap import DotMap
import numpy as np
from scipy.fft import dct, idct
import torch

from deepinv.physics.compressed_sensing import CompressedSensing
from deepinv.physics.forward import Physics, LinearPhysics
from deepinv.optim.phase_retrieval import compare,merge_order,spectral_methods

def dct2(x:torch.Tensor,device):
    r""" 2D DCT

    DCT is performed along the last two dimensions of the input tensor.
    """
    return torch.from_numpy(dct(dct(x.cpu().numpy(), axis=-1, norm='ortho'), axis=-2, norm='ortho')).to(device)

def idct2(x:torch.Tensor,device):
    r""" 2D IDCT

    IDCT is performed along the last two dimensions of the input tensor.
    """
    return torch.from_numpy(idct(idct(x.cpu().numpy(), axis=-2, norm='ortho'), axis=-1, norm='ortho')).to(device)

def triangular_distribution(a, size):
    u = torch.rand(size)  # Sample from uniform distribution [0, 1]
    
    # Apply inverse transform method for triangular distribution
    condition = (u < 0.5)
    samples = torch.zeros(size)
    
    # Left part of the triangular distribution
    samples[condition] = -a + torch.sqrt(u[condition] * 2 * a**2)
    
    # Right part of the triangular distribution
    samples[~condition] = a - torch.sqrt((1 - u[~condition]) * 2 * a**2)
    
    return samples

class MarchenkoPastur:
    def __init__(self,m,n,sigma=None):
        self.m = np.array(m)
        self.n = np.array(n)
        # when oversampling ratio is 1, the distribution has min support at 0, leading to a very high peak near 0 and numerical issues.
        self.gamma = np.array(n / m)
        if sigma is not None:
            self.sigma = np.array(sigma)
        else:
            # automatically set sigma to make E[|x|^2] = 1
            self.sigma = (1+self.gamma)**(-0.25)
        self.lamb = m / n
        self.min_supp = np.array(self.sigma**2*(1-np.sqrt(self.gamma))**2)
        self.max_supp = np.array(self.sigma**2*(1+np.sqrt(self.gamma))**2)
        self.max_pdf = None
    
    def pdf(self,x):
        assert (x >= self.min_supp).all() and (x <= self.max_supp).all(), "x is out of the support of the distribution"
        return np.sqrt((self.max_supp - x) * (x - self.min_supp)) / (2 * np.pi * self.sigma**2 * self.gamma * x)
    
    def sample(self,samples_shape):
        """using acceptance-rejection sampling"""
        # compute the maximum value of the pdf if not yet computed
        if self.max_pdf is None:
            self.max_pdf = np.max(self.pdf(np.linspace(self.min_supp,self.max_supp,10000)))
        
        samples = []
        while len(samples) < np.prod(samples_shape):
            x = np.random.uniform(self.min_supp, self.max_supp, size=1)
            y = np.random.uniform(0, self.max_pdf, size=1)
            if y < self.pdf(x):
                samples.append(x)
        return np.array(samples).reshape(samples_shape)
    
    def mean(self):
        return self.sigma**2
    
    def var(self):
        return self.gamma*self.sigma**4

def generate_diagonal(
    shape,
    mode,
    dtype=torch.complex64,
    device="cpu",
    config:DotMap=None,
):
    r"""
    Generate a random tensor as the diagonal matrix.
    """

    #! all distributions should be normalized to have E[|x|^2] = 1
    if mode == "uniform_phase":
        # Generate REAL-VALUED random numbers in the interval [0, 1)
        diag = torch.rand(shape)
        diag = 2 * np.pi * diag
        diag = torch.exp(1j * diag)
    elif mode == "uniform_magnitude":
        if config.range:
            diag = config.range * torch.rand(shape)
        else:
            # ensure E[|x|^2] = 1
            diag = torch.sqrt(torch.tensor(3.0)) * torch.rand(shape)
        diag = diag.to(dtype)
    elif mode == "gaussian":
        diag = torch.randn(shape, dtype=dtype)
    elif mode == "laplace":
        #! variance = 2*scale^2
        #! variance of complex numbers is doubled
        laplace_dist = torch.distributions.laplace.Laplace(0,0.5)
        diag = (laplace_dist.sample(shape) + 1j*laplace_dist.sample(shape))
    elif mode == "student-t":
        #! variance = df/(df-2) if df > 2
        #! variance of complex numbers is doubled
        student_t_dist = torch.distributions.studentT.StudentT(config.degree_of_freedom,0,1)
        scale = torch.sqrt((torch.tensor(config.degree_of_freedom)-2)/torch.tensor(config.degree_of_freedom)/2)
        diag = (scale*(student_t_dist.sample(shape) + 1j*student_t_dist.sample(shape))).to(device)
    elif mode == "marchenko":
        diag = torch.from_numpy(MarchenkoPastur(config.m,config.n).sample(shape)).to(dtype)
        diag = torch.sqrt(diag)
    elif mode == "uniform":
        #! variance = 1/2a for real numbers
        real = torch.sqrt(torch.tensor(6)) * (torch.rand(shape, dtype=torch.float32) - 0.5)
        imag = torch.sqrt(torch.tensor(6)) * (torch.rand(shape, dtype=torch.float32) - 0.5)
        diag = real + 1j*imag
    elif mode == "triangular":
        #! variance = a^2/6 for real numbers
        real = triangular_distribution(torch.sqrt(torch.tensor(3)),shape)
        imag = triangular_distribution(torch.sqrt(torch.tensor(3)),shape)
        diag = real + 1j*imag
    elif mode == "polar4":
        # generate random phase 1, -1, j, -j
        values = torch.tensor([1, -1, 1j, -1j])
        # Randomly select elements from the values with equal probability
        diag = values[torch.randint(0, len(values), shape)]
    else:
        raise ValueError(f"Unsupported mode: {mode}")
    if config.unit_mag is True:
        diag /= torch.abs(diag)
        assert torch.allclose(torch.abs(diag), torch.tensor(1.0)), "The magnitudes of the diagonal are not all 1s."
    if config.complex is False:
        diag = diag.real * torch.sqrt(torch.tensor(2.0)) # to ensure E[|x|^2] = 1
    return diag.to(device)

class PhaseRetrieval(Physics):
    r"""
    Phase Retrieval base class corresponding to the operator

    .. math::

        A(x) = |Bx|^2.

    The linear operator :math:`B` is defined by a :meth:`deepinv.physics.LinearPhysics` object.

    An existing operator can be loaded from a saved .pth file via ``self.load_state_dict(save_path)``, in a similar fashion to :class:`torch.nn.Module`.

    :param deepinv.physics.forward.LinearPhysics B: the linear forward operator.
    """

    def __init__(
        self,
        B: LinearPhysics,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.name = f"PR_m{self.m}"

        self.B = B

    def A(self, x: torch.Tensor) -> torch.Tensor:
        r"""
        Applies the forward operator to the input x.

        Note here the operation includes the modulus operation.

        :param torch.Tensor x: signal/image.
        """
        return self.B(x).abs().square()

    def A_dagger(self, y: torch.Tensor, **kwargs) -> torch.Tensor:
        r"""
        Computes a initial reconstruction for the image :math:`x` from the measurements :math:`y`.

        :param torch.Tensor y: measurements.
        :return: (torch.Tensor) an initial reconstruction for image :math:`x`.
        """
        return spectral_methods(y, self, **kwargs)

    def A_adjoint(self, y: torch.Tensor, **kwargs) -> torch.Tensor:
        return self.A_dagger(y, **kwargs)

    def B_adjoint(self, y: torch.Tensor) -> torch.Tensor:
        return self.B.A_adjoint(y)

    def B_dagger(self, y):
        r"""
        Computes the linear pseudo-inverse of :math:`B`.

        :param torch.Tensor y: measurements.
        :return: (torch.Tensor) the reconstruction image :math:`x`.
        """
        return self.B.A_dagger(y)

    def forward(self, x):
        r"""
        Applies the phase retrieval measurement operator, i.e. :math:`y = N(|Bx|^2)` (with noise :math:`N` and/or sensor non-linearities).

        :param torch.Tensor,list[torch.Tensor] x: signal/image
        :return: (torch.Tensor) noisy measurements
        """
        return self.sensor(self.noise(self.A(x)))

    def A_vjp(self, x, v):
        r"""
        Computes the product between a vector :math:`v` and the Jacobian of the forward operator :math:`A` at the input x, defined as:

        .. math::

            A_{vjp}(x, v) = 2 \overline{B}^{\top} diag(Bx) v.

        :param torch.Tensor x: signal/image.
        :param torch.Tensor v: vector.
        :return: (torch.Tensor) the VJP product between :math:`v` and the Jacobian.
        """
        return 2 * self.B_adjoint(self.B(x) * v)
    
    def release_memory(self):
        del self.B
        torch.cuda.empty_cache()
        return


class RandomPhaseRetrieval(PhaseRetrieval):
    r"""
    Random Phase Retrieval forward operator. Creates a random :math:`m \times n` sampling matrix :math:`B` where :math:`n` is the number of elements of the signal and :math:`m` is the number of measurements.

    This class generates a random i.i.d. Gaussian matrix

    .. math::

        B_{i,j} \sim \mathcal{N} \left( 0, \frac{1}{2m} \right) + \mathrm{i} \mathcal{N} \left( 0, \frac{1}{2m} \right).

    An existing operator can be loaded from a saved .pth file via ``self.load_state_dict(save_path)``, in a similar fashion to :class:`torch.nn.Module`.

    :param int m: number of measurements.
    :param tuple img_shape: shape (C, H, W) of inputs.
    :param bool channelwise: Channels are processed independently using the same random forward operator.
    :param torch.type dtype: Forward matrix is stored as a dtype. Default is torch.complex64.
    :param str device: Device to store the forward matrix.

    |sep|

    :Examples:

        Random phase retrieval operator with 10 measurements for a 3x3 image:

        >>> seed = torch.manual_seed(0) # Random seed for reproducibility
        >>> x = torch.randn((1, 1, 3, 3),dtype=torch.complex64) # Define random 3x3 image
        >>> physics = RandomPhaseRetrieval(m=10,img_shape=(1, 3, 3))
        >>> physics(x)
        tensor([[1.1901, 4.0743, 0.1858, 2.3197, 0.0734, 0.4557, 0.1231, 0.6597, 1.7768,
                 0.3864]])
    """

    def __init__(
        self,
        m,
        img_shape,
        channelwise=False,
        dtype=torch.complex64,
        device="cpu",
        config:DotMap=None,
        **kwargs,
    ):
        self.m = m
        self.input_shape = img_shape
        self.channelwise = channelwise
        self.dtype = dtype
        self.device = device
        B = CompressedSensing(
            m=m,
            img_shape=img_shape,
            fast=False,
            channelwise=channelwise,
            dtype=dtype,
            device=device,
            config=config,
        )
        super().__init__(B, **kwargs)
        self.name = f"RPR_m{self.m}"
    
    def get_A_squared_mean(self):
        return self.B._A.var() + self.B._A.mean()**2


class StructuredRandomPhaseRetrieval(PhaseRetrieval):
    r"""
    Pseudo-random Phase Retrieval class corresponding to the operator

    .. math::

        A(x) = |F \prod_{i=1}^N (D_i F) x|^2,

    where :math:`F` is the Discrete Fourier Transform (DFT) matrix, and :math:`D_i` are diagonal matrices with elements of unit norm and random phases, and :math:`N` is the number of layers.

    The phase of the diagonal elements of the matrices :math:`D_i` are drawn from a uniform distribution in the interval :math:`[0, 2\pi]`.

    :param int n_layers: number of layers. an extra F is at the end if there is a 0.5
    :param tuple img_shape: shape (C, H, W) of inputs.
    :param torch.type dtype: Signals are processed in dtype. Default is torch.complex64.
    :param str device: Device for computation.
    """

    def __init__(
        self,
        input_shape:tuple,
        output_shape:tuple,
        n_layers:int,
        transform="fft",
        diagonal_mode="uniform_phase", # right first
        distri_config:DotMap=None,
        shared_weights=False,
        dtype=torch.complex64,
        device="cpu",
        **kwargs,
    ):
        if output_shape is None:
            output_shape = input_shape

        height_order = compare(input_shape[1], output_shape[1])
        width_order = compare(input_shape[2], output_shape[2])

        order = merge_order(height_order, width_order)

        if order == "<":
            self.mode = "oversampling"
        elif order == ">":
            self.mode = "undersampling"
        elif order == "=":
            self.mode = "equisampling"
        else:
            raise ValueError(f"Does not support different sampling schemes on height and width.")
        
        change_top = math.ceil(abs(input_shape[1] - output_shape[1])/2)
        change_bottom = math.floor(abs(input_shape[1] - output_shape[1])/2)
        change_left = math.ceil(abs(input_shape[2] - output_shape[2])/2)
        change_right = math.floor(abs(input_shape[2] - output_shape[2])/2)
        assert change_top + change_bottom == abs(input_shape[1] - output_shape[1])
        assert change_left + change_right == abs(input_shape[2] - output_shape[2])

        def padding(tensor: torch.Tensor):
            return torch.nn.ZeroPad2d((change_left,change_right,change_top,change_bottom))(tensor)
        self.padding = padding

        def trimming(tensor: torch.Tensor):
            if change_bottom == 0:
                tensor = tensor[...,change_top:,:]
            else:
                tensor = tensor[...,change_top:-change_bottom,:]
            if change_right == 0:
                tensor = tensor[...,change_left:]
            else:
                tensor = tensor[...,change_left:-change_right]
            return tensor
        self.trimming = trimming

        self.input_shape = input_shape
        self.output_shape = output_shape
        self.n = torch.prod(torch.tensor(self.input_shape))
        self.m = torch.prod(torch.tensor(self.output_shape))
        self.oversampling_ratio = self.m / self.n
        assert n_layers % 1 == 0.5 or n_layers % 1 == 0, "n_layers must be an integer or an integer plus 0.5"
        self.n_layers = n_layers
        self.structure = self.get_structure(self.n_layers)
        self.shared_weights = shared_weights
        self.distri_config = distri_config
        self.distri_config.m = self.m
        self.distri_config.n = self.n

        self.dtype = dtype
        self.device = device

        self.diagonals = []

        if isinstance(diagonal_mode,str):
            diagonal_mode = [diagonal_mode] * math.floor(self.n_layers)
        
        if not shared_weights:
            for i in range(math.floor(self.n_layers)):
                if self.mode == "oversampling":
                    diagonal = generate_diagonal(self.output_shape, mode=diagonal_mode[i], dtype=self.dtype, device=self.device, config=self.distri_config)
                else:
                    diagonal = generate_diagonal(self.input_shape, mode=diagonal_mode[i], dtype=self.dtype, device=self.device, config=self.distri_config)
                self.diagonals.append(diagonal)
        else:
            if self.mode == "oversampling":
                diagonal = generate_diagonal(self.output_shape, mode=diagonal_mode[i], dtype=self.dtype, device=self.device, config=self.distri_config)
            else:
                diagonal = generate_diagonal(self.input_shape, mode=diagonal_mode[i], dtype=self.dtype, device=self.device, config=self.distri_config)
            self.diagonals = self.diagonals + [diagonal] * math.floor(self.n_layers)

        if transform == "fft":
            transform_func = partial(torch.fft.fft2, norm="ortho")
            transform_func_inv = partial(torch.fft.ifft2, norm="ortho")
        elif transform == "dct":
            transform_func = partial(dct2, device=self.device)
            transform_func_inv = partial(idct2, device=self.device)
        else:
            raise ValueError(f"Unimplemented transform: {transform}")
        
        def A(x):
            assert x.shape[1:] == self.input_shape, f"x doesn't have the correct shape {x.shape[1:]} != {self.input_shape}"

            if self.mode == "oversampling":
                x = self.padding(x)

            if (self.n_layers - math.floor(self.n_layers) == 0.5):
                x = transform_func(x)
            for i in range(math.floor(self.n_layers)):
                diagonal = self.diagonals[i]
                x = diagonal * x
                x = transform_func(x)

            if self.mode == "undersampling":
                x = self.trimming(x)

            return x

        def A_adjoint(y):
            assert y.shape[1:] == self.output_shape, f"y doesn't have the correct shape {y.shape[1:]} != {self.output_shape}"

            if self.mode == "undersampling":
                y = self.padding(y)

            for i in range(math.floor(self.n_layers)):
                diagonal = self.diagonals[-i - 1]
                y = transform_func_inv(y)
                y = torch.conj(diagonal) * y
            if (self.n_layers - math.floor(self.n_layers) == 0.5):
                y = transform_func_inv(y)

            if self.mode == "oversampling":
                y = self.trimming(y)

            return y

        super().__init__(LinearPhysics(A=A, A_adjoint=A_adjoint), **kwargs)
        self.name = f"PRPR_m{self.m}"

    def B_dagger(self, y):
        return self.B.A_adjoint(y)
    
    def get_A_squared_mean(self):
        if self.n_layers == 0.5:
            print("warning: computing the mean of the squared operator for a single Fourier transform.")
            return None
        return self.diagonals[0].var() + self.diagonals[0].mean()**2
    
    @staticmethod
    def get_structure(n_layers) -> str:
        return "FD" * math.floor(n_layers) + "F" * (n_layers % 1 == 0.5)