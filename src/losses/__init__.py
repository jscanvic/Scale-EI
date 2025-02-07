from torch.nn import Module
from deepinv.loss import SupLoss, EILoss
from deepinv.loss.metric import mse
from deepinv.transform import Rotate, Shift
from torch.nn.functional import l1_loss

from crop import CropPair
from transforms import ScalingTransform, CombinedTransform
from .r2r import R2REILoss
from .sure import SureGaussianLoss


class SupervisedLoss(Module):
    def __init__(self, physics):
        super().__init__()
        self.physics = physics
        metric = mse()
        from os import environ
        if "SUPERVISED_L1" in environ:
            print("SUPERVISED_L1")
            metric = l1_loss
        self.loss = SupLoss(metric=metric)

    def forward(self, x, y, model):
        x_net = model(y)
        return self.loss(x=x, x_net=x_net, y=y, physics=self.physics, model=model)


class CSSLoss(Module):
    def __init__(self, physics):
        super().__init__()
        self.physics = physics
        self.loss = SupLoss(metric=mse())

    def forward(self, x, y, model):
        x_net = model(y)
        return self.loss(x=x, x_net=x_net, y=y, physics=self.physics, model=model)


class Noise2InverseLoss(Module):
    def __init__(self, physics):
        super().__init__()
        self.physics = physics
        self.loss = SupLoss(metric=mse())

    def forward(self, x, y, model):
        x_net = model(y)
        return self.loss(x=x, x_net=x_net, y=y, physics=self.physics, model=model)


class SURELoss(Module):
    def __init__(self, noise_level, cropped_div, averaged_cst, margin, physics):
        super().__init__()
        self.physics = physics
        self.loss = SureGaussianLoss(
            sigma=noise_level / 255,
            cropped_div=cropped_div,
            averaged_cst=averaged_cst,
            margin=margin,
        )

    def forward(self, x, y, model):
        x_net = model(y)
        return self.loss(x=x, x_net=x_net, y=y, physics=self.physics, model=model)


class ProposedLoss(Module):
    def __init__(
        self,
        blueprint,
        sure_alternative,
        noise_level,
        stop_gradient,
        sure_cropped_div,
        sure_averaged_cst,
        sure_margin,
        alpha_tradeoff,
        transforms,
        physics,
    ):
        super().__init__()
        self.physics = physics

        if transforms == "Scaling_Transforms":
            ei_transform = ScalingTransform(**blueprint[ScalingTransform.__name__])
        elif transforms == "Rotations+Shifts":
            ei_transform = CombinedTransform([
                Rotate(),
                Shift(),
            ])
        elif transforms == "Rotations":
            ei_transform = Rotate()
        elif transforms == "Shifts":
            ei_transform = Shift()
        else:
            raise ValueError(f"Unknown transforms: {transforms}")

        assert sure_alternative in [None, "r2r"]
        if sure_alternative == "r2r":
            loss_fns = [
                R2REILoss(
                    transform=ei_transform,
                    sigma=noise_level / 255,
                    no_grad=stop_gradient,
                    metric=mse(),
                )
            ]
        else:
            sure_loss = SureGaussianLoss(
                sigma=noise_level / 255,
                cropped_div=sure_cropped_div,
                averaged_cst=sure_averaged_cst,
                margin=sure_margin,
            )
            loss_fns = [sure_loss]

            equivariant_loss = EILoss(
                metric=mse(),
                transform=ei_transform,
                no_grad=stop_gradient,
                weight=alpha_tradeoff,
            )

            loss_fns.append(equivariant_loss)
        self.loss_fns = loss_fns

        # NOTE: This could be done better.
        if sure_alternative == "r2r":
            self.compute_x_net = False
        else:
            self.compute_x_net = True

    def forward(self, x, y, model):
        if self.compute_x_net:
            x_net = model(y)
        else:
            x_net = None

        loss = 0
        for loss_fn in self.loss_fns:
            loss += loss_fn(x=x, x_net=x_net, y=y, physics=self.physics, model=model)
        return loss


class Loss(Module):
    def __init__(
        self,
        physics,
        blueprint,
        noise_level,
        sure_cropped_div,
        sure_averaged_cst,
        sure_margin,
        method,
        crop_training_pairs,
        crop_size,
    ):
        super().__init__()

        if method == "supervised":
            self.loss = SupervisedLoss(physics=physics)
        elif method == "css":
            self.loss = CSSLoss(physics=physics)
        elif method == "noise2inverse":
            self.loss = Noise2InverseLoss(physics=physics)
        elif method == "sure":
            self.loss = SURELoss(
                physics=physics,
                noise_level=noise_level,
                cropped_div=sure_cropped_div,
                averaged_cst=sure_averaged_cst,
                margin=sure_margin,
            )
        elif method == "proposed":
            self.loss = ProposedLoss(
                physics=physics,
                blueprint=blueprint,
                noise_level=noise_level,
                sure_cropped_div=sure_cropped_div,
                sure_averaged_cst=sure_averaged_cst,
                sure_margin=sure_margin,
                **blueprint[ProposedLoss.__name__],
            )
        else:
            raise ValueError(f"Unknwon method: {method}")

        if crop_training_pairs:
            if hasattr(physics, "rate"):
                self.xy_size_ratio = physics.rate
            else:
                self.xy_size_ratio = 1
            self.crop_fn = CropPair(location="random", size=crop_size)
        else:
            self.crop_fn = None
        from os import environ
        if "HOMOGENEOUS_SWINIR" in environ:
            if "_once453" not in globals():
                print("\nDo not crop training pairs as we process the loss\n")
                globals()["_once453"] = True
            self.crop_fn = None


    def forward(self, x, y, model):
        if self.crop_fn is not None:
            x, y = self.crop_fn(x, y, xy_size_ratio=self.xy_size_ratio)

        return self.loss(x=x, y=y, model=model)


def get_loss(args, physics):
    # NOTE: This is a bit of a mess.
    if args.partial_sure:
        if args.sure_margin is not None:
            sure_margin = args.sure_margin
        elif args.task == "deblurring":
            assert physics.task == "deblurring"

            kernel = physics.filter
            kernel_size = max(kernel.shape[-2], kernel.shape[-1])

            sure_margin = (kernel_size - 1) // 2
        elif args.task == "sr":
            if args.partial_sure_sr:
                sure_margin = 2
            else:
                sure_margin = 0
    else:
        assert args.sure_margin is None
        sure_margin = 0

    blueprint = {}

    blueprint[Loss.__name__] = {
        "crop_training_pairs": args.Loss__crop_training_pairs,
        "crop_size": args.Loss__crop_size,
    }

    blueprint[ProposedLoss.__name__] = {
        "stop_gradient": args.ProposedLoss__stop_gradient,
        "sure_alternative": args.ProposedLoss__sure_alternative,
        "alpha_tradeoff": args.ProposedLoss__alpha_tradeoff,
        "transforms": args.ProposedLoss__transforms,
    }

    blueprint[ScalingTransform.__name__] = {
        "kind": args.ScalingTransform__kind,
        "antialias": args.ScalingTransform__antialias,
    }

    method = args.method
    noise_level = args.noise_level
    sure_cropped_div = args.sure_cropped_div
    sure_averaged_cst = args.sure_averaged_cst

    loss = Loss(
        physics=physics,
        blueprint=blueprint,
        method=method,
        noise_level=noise_level,
        sure_cropped_div=sure_cropped_div,
        sure_averaged_cst=sure_averaged_cst,
        sure_margin=sure_margin,
        **blueprint[Loss.__name__],
    )

    return loss
