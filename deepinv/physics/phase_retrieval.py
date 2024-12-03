from functools import partial
import math

import numpy as np
import torch

from deepinv.optim.phase_retrieval import spectral_methods
from deepinv.physics.compressed_sensing import CompressedSensing
from deepinv.physics.forward import Physics, LinearPhysics
from deepinv.physics.structured_random import (
    compare,
    generate_diagonal,
    generate_spectrum,
    fft1,
    ifft1,
    fft2,
    ifft2,
    dct1,
    idct1,
    dct2,
    idct2,
    hadamard1,
    hadamard2,
    StructuredRandom,
)
from deepinv.optim.phase_retrieval import spectral_methods


class PhaseRetrieval(Physics):
    r"""
    Phase Retrieval base class corresponding to the operator

    .. math::

        \forw{x} = |Bx|^2.

    The linear operator :math:`B` is defined by a :class:`deepinv.physics.LinearPhysics` object.

    An existing operator can be loaded from a saved .pth file via ``self.load_state_dict(save_path)``, in a similar fashion to :class:`torch.nn.Module`.

    :param deepinv.physics.forward.LinearPhysics B: the linear forward operator.
    """

    def __init__(
        self,
        B: LinearPhysics,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.name = "Phase Retrieval"

        self.B = B

    def A(self, x: torch.Tensor, **kwargs) -> torch.Tensor:
        r"""
        Applies the forward operator to the input x.

        Note here the operation includes the modulus operation.

        :param torch.Tensor x: signal/image.
        """
        return self.B(x, **kwargs).abs().square()

    def A_dagger(self, y: torch.Tensor, **kwargs) -> torch.Tensor:
        r"""
        Computes a initial reconstruction for the image :math:`x` from the measurements :math:`y` using :class:`deepinv.optim.phase_retrieval.spectral_methods`.

        :param torch.Tensor y: measurements.
        :return: (torch.Tensor) an initial reconstruction for image :math:`x`.
        """
        return spectral_methods(y, self, **kwargs)

    def A_adjoint(self, y: torch.Tensor, **kwargs) -> torch.Tensor:
        return self.A_dagger(y, **kwargs)

    def B_adjoint(self, y: torch.Tensor, **kwargs) -> torch.Tensor:
        return self.B.A_adjoint(y, **kwargs)

    def B_dagger(self, y):
        r"""
        Computes the linear pseudo-inverse of :math:`B`.

        :param torch.Tensor y: measurements.
        :return: (torch.Tensor) the reconstruction image :math:`x`.
        """
        return self.B.A_dagger(y)

    def forward(self, x, **kwargs):
        r"""
        Applies the phase retrieval measurement operator, i.e. :math:`y = \noise{|Bx|^2}` (with noise :math:`N` and/or sensor non-linearities).

        :param torch.Tensor,list[torch.Tensor] x: signal/image
        :return: (torch.Tensor) noisy measurements
        """
        return self.sensor(self.noise(self.A(x, **kwargs)))

    def A_vjp(self, x, v):
        r"""
        Computes the product between a vector :math:`v` and the Jacobian of the forward operator :math:`A` at the input x, defined as:

        .. math::

            A_{vjp}(x, v) = 2 \overline{B}^{\top} \text{diag}(Bx) v.

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
    :param bool unitary: Use a random unitary matrix instead of Gaussian matrix. Default is False.
    :param bool compute_inverse: Compute the pseudo-inverse of the forward matrix. Default is False.
    :param torch.type dtype: Forward matrix is stored as a dtype. Default is torch.cfloat.
    :param str device: Device to store the forward matrix.
    :param torch.Generator (Optional) rng: a pseudorandom random number generator for the parameter generation.
        If ``None``, the default Generator of PyTorch will be used.

    |sep|

    :Examples:

        Random phase retrieval operator with 10 measurements for a 3x3 image:

        >>> seed = torch.manual_seed(0) # Random seed for reproducibility
        >>> x = torch.randn((1, 1, 3, 3),dtype=torch.cfloat) # Define random 3x3 image
        >>> physics = RandomPhaseRetrieval(m=6, img_shape=(1, 3, 3), rng=torch.Generator('cpu'))
        >>> physics(x)
        tensor([[3.8405, 2.2588, 0.0146, 3.0864, 1.8075, 0.1518]])

    """

    def __init__(
        self,
        m,
        img_shape,
        mode=None,
        product=1,
        unit_mag=False,
        compute_inverse=False,
        channelwise=False,
        dtype=torch.complex64,
        device="cpu",
        unitary=False,
        rng: torch.Generator = None,
        **kwargs,
    ):
        self.m = m
        self.input_shape = img_shape
        self.channelwise = channelwise
        self.dtype = dtype
        self.device = device
        if rng is None:
            self.rng = torch.Generator(device=device)
        else:
            # Make sure that the random generator is on the same device as the physic generator
            assert rng.device == torch.device(
                device
            ), f"The random generator is not on the same device as the Physics Generator. Got random generator on {rng.device} and the Physics Generator on {self.device}."
            self.rng = rng
        self.initial_random_state = self.rng.get_state()

        B = CompressedSensing(
            m=m,
            img_shape=img_shape,
            mode=mode,
            product=product,
            unit_mag=unit_mag,
            compute_inverse=compute_inverse,
            channelwise=channelwise,
            unitary=unitary,
            dtype=dtype,
            device=device,
            rng=self.rng,
        )
        super().__init__(B, **kwargs)
        self.name = "Random Phase Retrieval"

    def get_A_squared_mean(self):
        return self.B._A.var() + self.B._A.mean() ** 2


class StructuredRandomPhaseRetrieval(PhaseRetrieval):
    r"""
    Structured random phase retrieval model corresponding to the operator

    .. math::

        A(x) = |\prod_{i=1}^N (F D_i) x|^2,

    where :math:`F` is the Discrete Fourier Transform (DFT) matrix, and :math:`D_i` are diagonal matrices with elements of unit norm and random phases, and :math:`N` refers to the number of layers. It is also possible to replace :math:`x` with :math:`Fx` as an additional 0.5 layer.

    For oversampling, we first pad the input signal with zeros to match the output shape and pass it to :math:`A(x)`. For undersampling, we first pass the signal in its original shape to :math:`A(x)` and trim the output signal to match the output shape.

    The phase of the diagonal elements of the matrices :math:`D_i` are drawn from a uniform distribution in the interval :math:`[0, 2\pi]`.

    :param tuple input_shape: shape (C, H, W) of inputs.
    :param tuple output_shape: shape (C, H, W) of outputs.
    :param float n_layers: number of layers :math:`N`. If ``layers=N + 0.5``, a first :math:`F` transform is included, i.e., :math:`A(x)=|\prod_{i=1}^N (F D_i) F x|^2`.
    :param str transform: structured transform to use. Default is 'fft'.
    :param str diagonal_mode: sampling distribution for the diagonal elements. Default is 'uniform_phase'.
    :param bool shared_weights: if True, the same diagonal matrix is used for all layers. Default is False.
    :param torch.type dtype: Signals are processed in dtype. Default is torch.cfloat.
    :param str device: Device for computation. Default is `cpu`.
    """

    def __init__(
        self,
        input_shape: tuple,
        output_shape: tuple,
        n_layers: int,
        transform: str = "fourier2",
        diagonal_mode: list = [
            ["marchenko", "uniform"]
        ],  # lower index is closer to the input
        distri_config: dict = dict(),
        explicit_spectrum: str = "unit",
        pad_powers_of_two=False,
        shared_weights=False,
        dtype=torch.complex64,
        device="cpu",
        verbose=True,
        **kwargs,
    ):
        self.input_shape = input_shape
        self.output_shape = output_shape

        self.mode = compare(input_shape, output_shape)

        if "hadamard" in transform:
            pad_powers_of_two = True

        if pad_powers_of_two:
            middle_size = 2 ** math.ceil(
                math.log2(max(input_shape[1], output_shape[1]))
            )
            self.middle_shape = (1, middle_size, middle_size)
        elif self.mode == "oversampling":
            self.middle_shape = self.output_shape
        else:
            self.middle_shape = self.input_shape

        if verbose:
            print(f"middle shape: {self.middle_shape}")

        self.n = torch.prod(torch.tensor(self.input_shape))
        self.m = torch.prod(torch.tensor(self.output_shape))
        self.oversampling_ratio = self.m / self.n
        assert (
            n_layers % 1 == 0.5 or n_layers % 1 == 0
        ), "n_layers must be an integer or an integer plus 0.5"
        self.n_layers = n_layers
        self.structure = self.get_structure(self.n_layers)
        self.shared_weights = shared_weights
        self.transform = transform

        self.distri_config = distri_config
        self.distri_config["m"] = self.m
        self.distri_config["n"] = self.n

        self.dtype = dtype
        self.device = device

        #! spectrum shape will always be the input shape
        self.spectrum = generate_spectrum(
            shape=input_shape,
            mode=explicit_spectrum,
            config=self.distri_config,
            dtype=self.dtype,
            device=self.device,
            generator=torch.Generator(device=device),
        )

        # generate diagonal matrices
        self.diagonals = []

        if isinstance(diagonal_mode, str):
            diagonal_mode = [diagonal_mode] * math.floor(self.n_layers)

        if not shared_weights:
            for i in range(math.floor(self.n_layers)):
                diagonal = generate_diagonal(
                    self.middle_shape,
                    mode=diagonal_mode[i],
                    dtype=self.dtype,
                    device=self.device,
                    config=self.distri_config,
                )
                self.diagonals.append(diagonal)
        else:
            diagonal = generate_diagonal(
                self.middle_shape,
                mode=diagonal_mode[0],
                dtype=self.dtype,
                device=self.device,
                config=self.distri_config,
            )
            self.diagonals = self.diagonals + [diagonal] * math.floor(self.n_layers)

        if transform == "fourier1":
            transform_func = partial(fft1, device=self.device)
            transform_func_inv = partial(ifft1, device=self.device)
        elif transform == "fourier2":
            transform_func = partial(fft2, device=self.device)
            transform_func_inv = partial(ifft2, device=self.device)
        elif transform == "cosine1":
            transform_func = partial(dct1, device=self.device)
            transform_func_inv = partial(idct1, device=self.device)
        elif transform == "cosine2":
            transform_func = partial(dct2, device=self.device)
            transform_func_inv = partial(idct2, device=self.device)
        elif transform == "hadamard1":
            transform_func = hadamard1
            transform_func_inv = hadamard1
        elif transform == "hadamard2":
            transform_func = hadamard2
            transform_func_inv = hadamard2
        else:
            raise ValueError(f"Unimplemented transform: {transform}")

        B = StructuredRandom(
            mode=self.mode,
            spectrum=self.spectrum,
            input_shape=self.input_shape,
            output_shape=self.output_shape,
            middle_shape=self.middle_shape,
            n_layers=self.n_layers,
            transform=self.transform,
            transform_func=transform_func,
            transform_func_inv=transform_func_inv,
            diagonals=self.diagonals,
            **kwargs,
        )

        super().__init__(B, **kwargs)
        self.name = f"Structured Random Phase Retrieval"

    def B_dagger(self, y):
        return self.B.A_adjoint(y)

    def get_A_squared_mean(self):
        if self.n_layers == 0.5:
            print(
                "warning: computing the mean of the squared operator for a single Fourier transform."
            )
            return None
        return self.diagonals[0].var() + self.diagonals[0].mean() ** 2

    @staticmethod
    def get_structure(n_layers) -> str:
        r"""Returns the structure of the operator as a string.

        :param float n_layers: number of layers.

        :return: (str) the structure of the operator, e.g., "FDFD".
        """
        return "FD" * math.floor(n_layers) + "F" * (n_layers % 1 == 0.5)

    def get_singular_values(self):
        r"""Returns the singular values of the forward matrix.

        :return: (torch.Tensor) the singular values.
        """
        return self.B.get_singular_values()
