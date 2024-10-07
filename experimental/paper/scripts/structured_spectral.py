import sys

sys.path.append("/home/zhhu/workspaces/deepinv/")

from datetime import datetime
from pathlib import Path
import shutil

from dotmap import DotMap
import numpy as np
import pandas as pd
import torch
from tqdm import trange
import yaml

import deepinv as dinv
from deepinv.utils.demo import load_url_image, get_image_url
from deepinv.utils.plotting import plot
from deepinv.optim.phase_retrieval import (
    cosine_similarity,
    spectral_methods,
    load_config,
)

# load config
config_path = "../config/structured_spectral.yaml"
config = load_config(config_path)

# general
model_name = config.general.name
recon = config.general.recon
save = config.general.save

# model
img_size = config.image.img_size
n_layers = config.model.n_layers
drop_tail = config.model.drop_tail
transform = config.model.transform
diagonal_mode = config.model.diagonal.mode
distri_config = config.model.diagonal.config
shared_weights = config.model.shared_weights

# recon
n_repeats = config.recon.n_repeats
max_iter = config.recon.max_iter

if config.recon.series == "arange":
    start = config.recon.start
    end = config.recon.end
    output_sizes = torch.arange(start, end, 2)
elif config.recon.series == "list":
    output_sizes = torch.tensor(config.recon.list)
else:
    raise ValueError("Invalid series type.")

oversampling_ratios = output_sizes**2 / img_size**2
n_oversampling = oversampling_ratios.shape[0]

# save
if save:
    res_name = config.save.name.format(
        model_name = model_name,
        # keep 4 digits of the following numbers
        oversampling_start = np.round(oversampling_ratios[0].numpy(),4),
        oversampling_end = np.round(oversampling_ratios[-1].numpy(),4),
        recon = recon,
        n_repeats = n_repeats,
        )
    print("res_name:", res_name)

    current_time = datetime.now().strftime("%Y%m%d-%H%M%S")
    SAVE_DIR = Path(config.save.path)
    SAVE_DIR = SAVE_DIR / current_time
    Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)
    print("save directory:", SAVE_DIR)

    # copy the config file to the save directory
    shutil.copy(config_path, SAVE_DIR / "config.yaml")

device = dinv.utils.get_freer_gpu() if torch.cuda.is_available() else "cpu"
device


# Set up the image to be reconstructed.
img_size = config.image.img_size
if config.image.mode == "shepp-logan":
    url = get_image_url("SheppLogan.png")
    img = load_url_image(
    url=url, img_size=img_size, grayscale=True, resize_mode="resize", device=device
    )
    print(img.shape)
elif config.image.mode == "random":
    # random phase signal
    img = torch.rand((1, 1, img_size, img_size), device=device)
elif config.image.mode == "mix":
    url = get_image_url("SheppLogan.png")
    img = load_url_image(
    url=url, img_size=img_size, grayscale=True, resize_mode="resize", device=device
    )
    img = img * (1-config.image.noise_ratio) + torch.rand_like(img) * config.image.noise_ratio
else:
    raise ValueError("Invalid image mode.")

# visualize the image
plot(img)

# generate phase signal
# The phase is computed as 2*pi*x - pi, where x is the original image.
x = torch.exp(1j * img * torch.pi - 0.5j * torch.pi).to(device)
# Every element of the signal should have unit norm.
assert torch.allclose(x.real**2 + x.imag**2, torch.tensor(1.0))

df_res = pd.DataFrame(
    {
        "oversampling_ratio": oversampling_ratios,
        **{f"repeat{i}": None for i in range(n_repeats)},
    }
)

for i in trange(n_oversampling):
    oversampling_ratio = oversampling_ratios[i]
    output_size = output_sizes[i]
    print(f"output_size: {output_size}")
    print(f"oversampling_ratio: {oversampling_ratio}")
    for j in range(n_repeats):
        physics = dinv.physics.StructuredRandomPhaseRetrieval(
            input_shape=(1, img_size, img_size),
            output_shape=(1, output_size, output_size),
            n_layers=n_layers,
            drop_tail=drop_tail,
            transform=transform,
            diagonal_mode=diagonal_mode,
            distri_config=distri_config,
            shared_weights=shared_weights,
            dtype=torch.complex64,
            device=device,
        )
        y = physics(x)

        x_phase_spec = spectral_methods(y, physics, n_iter=max_iter)
        df_res.loc[i, f"repeat{j}"] = cosine_similarity(x, x_phase_spec).item()
        # print the cosine similarity
        print(f"cosine similarity: {df_res.loc[i, f'repeat{j}']}")

# save results
if save:
    df_res.to_csv(SAVE_DIR / res_name)    
