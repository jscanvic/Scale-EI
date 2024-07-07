import torch
from torch.nn import Module
from torch.utils.data import Dataset as BaseDataset
from torchvision.transforms import InterpolationMode, functional as TF
from functools import wraps

from noise2inverse import Noise2InverseTransform
from .crop import CropPair
from .div2k import Div2K
from .tomography import TomographyDataset
from .urban100 import Urban100
from .single_image import SingleImageDataset


class GroundTruthDataset(BaseDataset):
    def __init__(
        self,
        blueprint,
        datasets_dir,
        dataset_name,
        split,
        download,
        size,
        device,
        memoize_gt,
    ):
        super().__init__()
        self.size = size
        self.device = device
        self.memoize_gt = memoize_gt

        if dataset_name == "div2k":
            self.dataset = Div2K(split, datasets_dir, download=download)
        elif dataset_name == "urban100":
            self.dataset = Urban100(split, datasets_dir, download=download)
        elif dataset_name == "ct":
            self.dataset = TomographyDataset(split, datasets_dir, download=download)
        elif dataset_name == "single_image":
            self.dataset = SingleImageDataset(**blueprint[SingleImageDataset.__name__])
        else:
            raise ValueError(f"Unknown dataset: {dataset_name}")

    def get_unique_id(self, index):
        if hasattr(self.dataset, "get_unique_id"):
            id = self.dataset.get_unique_id(index)
        else:
            id = index
        return id

    @staticmethod
    def memoize_load_image(f):
        cache = {}

        @wraps(f)
        def wrapper(*args, **kwargs):
            self = args[0]
            if not self.memoize_gt:
                x = f(*args, **kwargs)
            else:
                key = (args, frozenset(kwargs.items()))
                if key not in cache:
                    x = f(*args, **kwargs)
                    device = x.device
                    x = x.to("cpu")
                    cache[key] = (device, x)
                device, x = cache[key]
                x = x.to(device)
            return x

        return wrapper

    @memoize_load_image
    def __getitem__(self, index):
        x = self.dataset[index]
        x = x.to(self.device)
        if self.size is not None:
            x = TF.resize(
                x,
                size=self.size,
                interpolation=InterpolationMode.BICUBIC,
                antialias=True,
            )

        return x

    def __len__(self):
        return len(self.dataset)


# NOTE: Getting small random crops should be optional and it should
# be possible to use big crops instead, e.g. to make images
# square-shaped which would enable stacking in the batch dimension.


class PrepareTrainingPairs(Module):
    def __init__(self, physics, crop_size=48, crop_location="random"):
        super().__init__()
        self.physics = physics
        self.crop_size = crop_size
        self.crop_location = crop_location

    def forward(self, x, y):
        # NOTE: It'd be great if physics contained its own downsampling ratio
        # even for a blur operator.

        if self.physics.task == "sr":
            xy_size_ratio = self.physics.ratio
        else:
            xy_size_ratio = 1

        T_crop = CropPair(location=self.crop_location, size=self.crop_size)
        return T_crop(x, y, xy_size_ratio=xy_size_ratio)


class Dataset(BaseDataset):
    def __init__(
        self,
        blueprint,
        purpose,
        physics,
        css,
        noise2inverse,
        split,
        device,
        memoize_gt,
        unique_seeds,
    ):
        super().__init__()
        self.purpose = purpose

        # NOTE: The two are meant to be combined.
        self.physics = physics
        self.physics_manager = self.physics.__manager

        self.css = css
        self.noise2inverse = noise2inverse

        # NOTE: the measurements should always be deterministic except for
        # supervised training. Although it can be dealt with in the loss as well.
        self.deterministic_measurements = purpose == "test"
        self.unique_seeds = unique_seeds

        self.ground_truth_dataset = GroundTruthDataset(
            blueprint=blueprint,
            device=device,
            split=split,
            memoize_gt=memoize_gt,
            **blueprint[GroundTruthDataset.__name__],
        )

        self.prepare_training_pairs = PrepareTrainingPairs(
            physics=self.physics,
            **blueprint[PrepareTrainingPairs.__name__],
        )

    def __len__(self):
        return len(self.ground_truth_dataset)

    def __getitem__(self, index):
        x = self.ground_truth_dataset[index]

        # NOTE: This should ideally be done in the class CSSLoss instead but
        # the border effects in the current implementation make it challenging.
        if self.css:
            x = x.unsqueeze(0)
            x = self.physics_manager.randomly_degrade(x, seed=None)
            x = x.squeeze(0)

        if self.deterministic_measurements:
            if self.unique_seeds:
                seed = self.ground_truth_dataset.get_unique_id(index)
            else:
                seed = 0
        else:
            seed = None

        x = x.unsqueeze(0)
        y = self.physics_manager.randomly_degrade(x, seed=seed)
        y = y.squeeze(0)
        x = x.squeeze(0)

        if self.purpose == "train":
            # NOTE: This should ideally be done in the model.
            degradation_inverse_fn = self.physics.A_dagger
            if self.noise2inverse:
                physics_filter = getattr(self.physics, "filter", None)
                T_n2i = Noise2InverseTransform(
                    task=self.physics.task,
                    physics_filter=physics_filter,
                    degradation_inverse_fn=degradation_inverse_fn,
                )
                x, y = T_n2i(x.unsqueeze(0), y.unsqueeze(0))
                x = x.squeeze(0)
                y = y.squeeze(0)

            # NOTE: This should ideally either be done in the model, or not at
            # all.
            x, y = self.prepare_training_pairs(x, y)
        elif self.purpose == "test":
            # NOTE: This should ideally be removed.
            if self.noise2inverse:
                # bug fix: make y have even height and width
                if self.physics.task == "deblurring":
                    w = 2 * (y.shape[1] // 2)
                    h = 2 * (y.shape[2] // 2)
                    y = y[:, :w, :h]

            # NOTE: This should ideally be removed.
            # crop x to make its dimensions be a multiple of u's dimensions
            if x.shape != y.shape:
                h, w = y.shape[1], y.shape[2]
                if self.physics.task == "sr":
                    f = self.physics.ratio
                else:
                    f = 1
                x = TF.crop(x, top=0, left=0, height=h * f, width=w * f)
        else:
            raise ValueError(f"Unknown purpose: {self.purpose}")

        return x, y


def get_dataset(args, purpose, physics, device):
    if purpose == "test":
        noise2inverse = args.noise2inverse
        css = False
        split = args.split
        memoize_gt = False
    elif purpose == "train":
        noise2inverse = args.method == "noise2inverse"
        css = args.method == "css"
        split = "train"
        memoize_gt = args.memoize_gt
    else:
        raise ValueError(f"Unknown purpose: {purpose}")

    blueprint = {}

    blueprint[GroundTruthDataset.__name__] = {
        # NOTE: This argument should be named according to the class
        # GroundTruthDataset but happens to be used (wrongly) elsewhere and
        # this must be dealt with first
        "dataset_name": args.dataset,
        "datasets_dir": args.GroundTruthDataset__datasets_dir,
        "download": args.GroundTruthDataset__download,
        "size": args.GroundTruthDataset__size,
    }

    blueprint[PrepareTrainingPairs.__name__] = {
        "crop_size": args.PrepareTrainingPairs__crop_size,
        "crop_location": args.PrepareTrainingPairs__crop_location,
    }

    blueprint[SingleImageDataset.__name__] = {
        "image_path": args.SingleImageDataset__image_path,
        "duplicates_count": args.SingleImageDataset__duplicates_count,
    }

    blueprint[Dataset.__name__] = {
        "unique_seeds": args.Dataset__unique_seeds,
    }

    return Dataset(
        blueprint=blueprint,
        device=device,
        physics=physics,
        purpose=purpose,
        css=css,
        noise2inverse=noise2inverse,
        split=split,
        memoize_gt=memoize_gt,
        **blueprint[Dataset.__name__],
    )
