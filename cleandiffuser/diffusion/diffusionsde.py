from typing import Optional, Union, Callable, Dict

import numpy as np
import torch
import torch.nn as nn
import einops

from cleandiffuser.classifier import BaseClassifier
from cleandiffuser.nn_condition import BaseNNCondition
from cleandiffuser.nn_diffusion import BaseNNDiffusion
from cleandiffuser.utils import (
    at_least_ndim,
    TensorDict,
    dict_apply,
    concat_zeros,
    SUPPORTED_NOISE_SCHEDULES,
    SUPPORTED_DISCRETIZATIONS,
    SUPPORTED_SAMPLING_STEP_SCHEDULE,
)
from .basic import DiffusionModel

SUPPORTED_SOLVERS = [
    "ddpm",
    "ddim",
    "ode_dpmsolver_1",
    "ode_dpmsolver++_1",
    "ode_dpmsolver++_2M",
    "sde_dpmsolver_1",
    "sde_dpmsolver++_1",
    "sde_dpmsolver++_2M",
]


def epstheta_to_xtheta(x, alpha, sigma, eps_theta):
    """
    x_theta = (x - sigma * eps_theta) / alpha
    """
    return (x - sigma * eps_theta) / alpha


def xtheta_to_epstheta(x, alpha, sigma, x_theta):
    """
    eps_theta = (x - alpha * x_theta) / sigma
    """
    return (x - alpha * x_theta) / sigma


class BaseDiffusionSDE(DiffusionModel):
    def __init__(
        self,
        # ----------------- Neural Networks ----------------- #
        nn_diffusion: BaseNNDiffusion,
        nn_condition: Optional[BaseNNCondition] = None,
        # ----------------- Masks ----------------- #
        fix_mask: Optional[torch.Tensor] = None,
        loss_weight: Optional[torch.Tensor] = None,
        # ------------------ Plugins ---------------- #
        classifier: Optional[BaseClassifier] = None,
        # ------------------ Training Params ---------------- #
        ema_rate: float = 0.995,
        # ------------------- Diffusion Params ------------------- #
        epsilon: float = 1e-3,
        x_max: Optional[torch.Tensor] = None,
        x_min: Optional[torch.Tensor] = None,
        predict_noise: bool = True,
    ):
        super().__init__(nn_diffusion, nn_condition, fix_mask, loss_weight, classifier, ema_rate)

        self.predict_noise = predict_noise
        self.epsilon = epsilon
        self.x_max = nn.Parameter(x_max, requires_grad=False) if x_max is not None else None
        self.x_min = nn.Parameter(x_min, requires_grad=False) if x_min is not None else None

    @property
    def supported_solvers(self):
        return SUPPORTED_SOLVERS

    @property
    def clip_pred(self):
        return (self.x_max is not None) or (self.x_min is not None)

    # ==================== Training: Score Matching ======================

    def loss(self, x0: torch.Tensor, condition: Optional[Union[torch.Tensor, TensorDict]] = None):
        xt, t, eps = self.add_noise(x0)

        condition = self.model["condition"](condition) if condition is not None else None

        if self.predict_noise:
            loss = (self.model["diffusion"](xt, t, condition) - eps) ** 2
        else:
            loss = (self.model["diffusion"](xt, t, condition) - x0) ** 2

        return (loss * self.loss_weight * (1 - self.fix_mask)).mean()

    # ==================== Sampling: Solving SDE/ODE ======================

    def classifier_guidance(self, xt, t, alpha, sigma, model, condition=None, w: float = 1.0, pred=None):
        """
        Guided Sampling CG:
        bar_eps = eps - w * sigma * grad
        bar_x0  = x0 + w * (sigma ** 2) * alpha * grad
        """
        if pred is None:
            pred = model["diffusion"](xt, t, None)
        if self.classifier is None or w == 0.0:
            return pred, None
        else:
            log_p, grad = self.classifier.gradients(xt.clone(), t, condition)
            if self.predict_noise:
                pred = pred - w * sigma * grad
            else:
                pred = pred + w * ((sigma**2) / alpha) * grad

        return pred, log_p

    def classifier_free_guidance(
        self,
        xt: torch.Tensor,
        t: torch.Tensor,
        model: BaseNNDiffusion,
        condition: Optional[Union[torch.Tensor, TensorDict]] = None,
        w: float = 0.0,
        pred: Optional[torch.Tensor] = None,
        pred_uncond: Optional[torch.Tensor] = None,
        requires_grad: bool = False,
    ):
        """Classifier-Free Guided Sampling.

        Add classifier-free guidance to the network prediction
        (epsilon_theta for `predict_noise` and x0_theta for not `predict_noise`).

        Args:
            xt (torch.Tensor):
                Noisy data at time `t`. (bs, *x_shape)
            t (torch.Tensor):
                Diffusion timestep. (bs, )
            model (BaseNNDiffusion):
                Diffusion model network backbone.
            condition (Optional[Union[torch.Tensor, TensorDict]]):
                CFG Condition. It is a tensor or a TensorDict that can be handled by `nn_condition`.
                Defaults to None.
            w (float):
                Guidance strength. Defaults to 0.0.
            pred (Optional[torch.Tensor]):
                Network prediction at time `t` for conditioned model. Defaults to None.
            pred_uncond (Optional[torch.Tensor]):
                Network prediction at time `t` for unconditioned model. Defaults to None.
            requires_grad (bool):
                Whether to calculate gradients. Defaults to False.

        Returns:
            pred (torch.Tensor):
                Prediction with classifier-free guidance. (bs, *x_shape)
        """
        with torch.set_grad_enabled(requires_grad):
            # fully conditioned prediction
            if w == 1.0:
                pred = model["diffusion"](xt, t, condition)
                pred_uncond = 0.0

            # unconditioned prediction
            elif w == 0.0:
                pred = 0.0
                pred_uncond = model["diffusion"](xt, t, None)

            # classifier-free guidance
            else:
                if pred is None or pred_uncond is None:
                    if isinstance(condition, dict):
                        condition = dict_apply(condition, concat_zeros, dim=0)
                    elif isinstance(condition, torch.Tensor):
                        condition = concat_zeros(condition, dim=0)
                    else:
                        raise ValueError("Condition must be either Tensor or TensorDict.")

                    pred_all = model["diffusion"](einops.repeat(xt, "b ... -> (2 b) ..."), t.repeat(2), condition)

                    pred, pred_uncond = torch.chunk(pred_all, 2, dim=0)

        bar_pred = w * pred + (1 - w) * pred_uncond

        return bar_pred

    def clip_prediction(self, pred, xt, alpha, sigma):
        """
        Clip the prediction at each sampling step to stablize the generation.
        (xt - alpha * x_max) / sigma <= eps <= (xt - alpha * x_min) / sigma
                               x_min <= x0  <= x_max
        """
        if self.predict_noise:
            if self.clip_pred:
                upper_bound = (xt - alpha * self.x_min) / sigma if self.x_min is not None else None
                lower_bound = (xt - alpha * self.x_max) / sigma if self.x_max is not None else None
                pred = pred.clip(lower_bound, upper_bound)
        else:
            if self.clip_pred:
                pred = pred.clip(self.x_min, self.x_max)

        return pred

    def guided_sampling(
        self,
        xt: torch.Tensor,
        t: torch.Tensor,
        alpha: torch.Tensor,
        sigma: torch.Tensor,
        model: BaseNNDiffusion,
        condition_cfg: Optional[Union[torch.Tensor, TensorDict]] = None,
        w_cfg: float = 0.0,
        condition_cg: Optional[Union[torch.Tensor, TensorDict]] = None,
        w_cg: float = 0.0,
        requires_grad: bool = False,
    ):
        """
        One-step epsilon/x0 prediction with guidance.
        """

        pred = self.classifier_free_guidance(xt, t, model, condition_cfg, w_cfg, None, None, requires_grad)

        pred, logp = self.classifier_guidance(xt, t, alpha, sigma, model, condition_cg, w_cg, pred)

        return pred, logp

    def sample(self, *args, **kwargs):
        raise NotImplementedError


class DiscreteDiffusionSDE(BaseDiffusionSDE):
    """Discrete-time Diffusion SDE

    The Diffusion SDE is currently one of the most commonly used formulations of diffusion processes.
    Its training process involves utilizing neural networks to estimate its scaled score function,
    which is used to compute the reverse process. The Diffusion SDE has reverse processes
    in both SDE and ODE forms, sharing the same marginal distribution.
    The first-order discretized forms of both are equivalent to well-known models such as DDPM and DDIM.
    DPM-Solvers have observed the semi-linearity of the reverse process and have computed its exact solution.

    The DiscreteDiffusionSDE is the discrete-time version of the Diffusion SDE.
    It discretizes the continuous time interval into a finite number of diffusion steps
    and only estimates the score function on these steps.
    Therefore, in sampling, solvers can only work on these learned steps.
    The sampling steps are required to be greater than 1 and less than or equal to the number of diffusion steps.

    Args:
        nn_diffusion: BaseNNDiffusion
            The neural network backbone for the Diffusion model.
        nn_condition: Optional[BaseNNCondition]
            The neural network backbone for the condition embedding.
        fix_mask: Union[list, np.ndarray, torch.Tensor]
            Fix some portion of the input data, and only allow the diffusion model to complete the rest part.
            The mask should be in the shape of `x_shape`.
        loss_weight: Union[list, np.ndarray, torch.Tensor]
            Add loss weight. The weight should be in the shape of `x_shape`.
        classifier: Optional[BaseClassifier]
            Add a classifier to enable classifier-guidance.
        grad_clip_norm: Optional[float]
            Gradient clipping norm.
        ema_rate: float
            Exponential moving average rate.
        optim_params: Optional[dict]
            Optimizer parameters.
        epsilon: float
            The minimum time step for the diffusion reverse process.
            In practice, using a very small value instead of `0` can avoid numerical instability.
        diffusion_steps: int
            The discretization steps for discrete-time diffusion models.
        discretization: Union[str, Callable]
            The discretization method for the diffusion steps.

        noise_schedule: Union[str, Dict[str, Callable]]
            The noise schedule for the diffusion process. Can be "linear" or "cosine".
        noise_schedule_params: Optional[dict]
            The parameters for the noise schedule.

        x_max: Optional[torch.Tensor]
            The maximum value for the input data. `None` indicates no constraint.
        x_min: Optional[torch.Tensor]
            The minimum value for the input data. `None` indicates no constraint.

        predict_noise: bool
            Whether to predict the noise or the data.

        device: Union[torch.device, str]
            The device to run the model.
    """

    def __init__(
        self,
        # ----------------- Neural Networks ----------------- #
        nn_diffusion: BaseNNDiffusion,
        nn_condition: Optional[BaseNNCondition] = None,
        # ----------------- Masks ----------------- #
        fix_mask: Optional[torch.Tensor] = None,  # be in the shape of `x_shape`
        loss_weight: Optional[torch.Tensor] = None,  # be in the shape of `x_shape`
        # ------------------ Plugins ---------------- #
        # Add a classifier to enable classifier-guidance
        classifier: Optional[BaseClassifier] = None,
        # ------------------ Training Params ---------------- #
        ema_rate: float = 0.995,
        # ------------------- Diffusion Params ------------------- #
        epsilon: float = 1e-3,
        x_max: Optional[torch.Tensor] = None,
        x_min: Optional[torch.Tensor] = None,
        predict_noise: bool = True,
        diffusion_steps: int = 1000,
        discretization: Union[str, Callable] = "uniform",
        noise_schedule: Union[str, Dict[str, Callable]] = "cosine",
        noise_schedule_params: Optional[dict] = None,
    ):
        super().__init__(
            nn_diffusion,
            nn_condition,
            fix_mask,
            loss_weight,
            classifier,
            ema_rate,
            epsilon,
            x_max,
            x_min,
            predict_noise,
        )

        self.save_hyperparameters(ignore=["nn_diffusion", "nn_condition", "classifier"])

        self.diffusion_steps = diffusion_steps

        # ================= Discretization =================
        # - Map the continuous range [epsilon, 1.] to the discrete range [0, T-1]
        # `t_diffusion` is a tensor with shape (T,), recording the `t` for each discrete step.
        if isinstance(discretization, str):
            if discretization in SUPPORTED_DISCRETIZATIONS.keys():
                t_diffusion = SUPPORTED_DISCRETIZATIONS[discretization](diffusion_steps, epsilon)
            else:
                raise ValueError(f"Discretization {discretization} is not supported.")
        elif callable(discretization):
            t_diffusion = discretization(diffusion_steps, epsilon)
        else:
            raise ValueError("discretization must be a callable or a string")
        self.t_diffusion = nn.Parameter(t_diffusion, requires_grad=False)

        # ================= Noise Schedule =================
        # - The noise schedule `alpha` and `sigma` for each discrete step.
        # - Both `alpha` and `sigma` are tensors with shape (T,)
        if isinstance(noise_schedule, str):
            if noise_schedule in SUPPORTED_NOISE_SCHEDULES.keys():
                alpha, sigma = SUPPORTED_NOISE_SCHEDULES[noise_schedule]["forward"](
                    t_diffusion, **(noise_schedule_params or {})
                )
            else:
                raise ValueError(f"Noise schedule {noise_schedule} is not supported.")
        elif isinstance(noise_schedule, dict):
            alpha, sigma = noise_schedule["forward"](self.t_diffusion, **(noise_schedule_params or {}))
        else:
            raise ValueError("noise_schedule must be a dict of callable or a string")
        self.alpha = nn.Parameter(alpha, requires_grad=False)
        self.sigma = nn.Parameter(sigma, requires_grad=False)
        self.logSNR = nn.Parameter(torch.log(self.alpha / self.sigma), requires_grad=False)

    # ==================== Training: Score Matching ======================

    def add_noise(self, x0: torch.Tensor, t: Optional[torch.Tensor] = None, eps: Optional[torch.Tensor] = None):
        """Forward process of the diffusion model.

        Args:
            x0: torch.Tensor,
                Samples from the target distribution. shape: (batch_size, *x_shape)
            t: torch.Tensor,
                Discrete time steps for the diffusion process. shape: (batch_size,).
                If `None`, randomly sample from Uniform(0, T).
            eps: torch.Tensor,
                Noise for the diffusion process. shape: (batch_size, *x_shape).
                If `None`, randomly sample from Normal(0, I).

        Returns:
            xt: torch.Tensor,
                The noisy samples. shape: (batch_size, *x_shape).
            t: torch.Tensor,
                The discrete time steps. shape: (batch_size,).
            eps: torch.Tensor,
                The noise. shape: (batch_size, *x_shape).
        """
        t = t or torch.randint(self.diffusion_steps, (x0.shape[0],), device=self.device)
        eps = eps or torch.randn_like(x0)

        alpha, sigma = at_least_ndim(self.alpha[t], x0.dim()), at_least_ndim(self.sigma[t], x0.dim())

        xt = alpha * x0 + sigma * eps
        xt = (1.0 - self.fix_mask) * xt + self.fix_mask * x0

        return xt, t, eps

    # ==================== Sampling: Solving SDE/ODE ======================

    def sample(
        self,
        # ---------- the known fixed portion ---------- #
        prior: torch.Tensor,
        # ----------------- sampling ----------------- #
        solver: str = "ddpm",
        n_samples: int = 1,
        sample_steps: int = 5,
        sample_step_schedule: Union[str, Callable] = "uniform",
        use_ema: bool = True,
        temperature: float = 1.0,
        # ------------------ guidance ------------------ #
        condition_cfg=None,
        mask_cfg=None,
        w_cfg: float = 0.0,
        condition_cg=None,
        w_cg: float = 0.0,
        # ----------- Diffusion-X sampling ----------
        diffusion_x_sampling_steps: int = 0,
        # ----------- Warm-Starting -----------
        warm_start_reference: Optional[torch.Tensor] = None,
        warm_start_forward_level: float = 0.3,
        # ------------------ others ------------------ #
        requires_grad: bool = False,
        preserve_history: bool = False,
        **kwargs,
    ):
        """Sampling.

        Inputs:
        - prior: torch.Tensor
            The known fixed portion of the input data. Should be in the shape of generated data.
            Use `torch.zeros((n_samples, *x_shape))` for non-prior sampling.

        - solver: str
            The solver for the reverse process. Check `supported_solvers` property for available solvers.
        - n_samples: int
            The number of samples to generate.
        - sample_steps: int
            The number of sampling steps. Should be greater than 1 and less than or equal to the number of diffusion steps.
        - sample_step_schedule: Union[str, Callable]
            The schedule for the sampling steps.
        - use_ema: bool
            Whether to use the exponential moving average model.
        - temperature: float
            The temperature for sampling.

        - condition_cfg: Optional
            Condition for Classifier-free-guidance.
        - mask_cfg: Optional
            Mask for Classifier-guidance.
        - w_cfg: float
            Weight for Classifier-free-guidance.
        - condition_cg: Optional
            Condition for Classifier-guidance.
        - w_cg: float
            Weight for Classifier-guidance.

        - diffusion_x_sampling_steps: int
            The number of diffusion steps for diffusion-x sampling.

        - warm_start_reference: Optional[torch.Tensor]
            Reference data for warm-starting sampling. `None` indicates no warm-starting.
        - warm_start_forward_level: float
            The forward noise level to perturb the reference data. Should be in the range of `[0., 1.]`, where `1` indicates pure noise.

        - requires_grad: bool
            Whether to preserve gradients.
        - preserve_history: bool
            Whether to preserve the sampling history.

        Outputs:
        - x0: torch.Tensor
            Generated samples. Be in the shape of `(n_samples, *x_shape)`.
        - log: dict
            The log dictionary.
        """
        assert solver in SUPPORTED_SOLVERS, f"Solver {solver} is not supported."

        # ===================== Initialization =====================
        log = {
            "sample_history": np.empty((n_samples, sample_steps + 1, *prior.shape)) if preserve_history else None,
        }

        model = self.model if not use_ema else self.model_ema

        prior = prior.to(self.device)
        if isinstance(warm_start_reference, torch.Tensor) and 0 < warm_start_forward_level < 1:
            diffusion_steps = int(warm_start_forward_level * self.diffusion_steps)
            fwd_alpha, fwd_sigma = self.alpha[diffusion_steps], self.sigma[diffusion_steps]
            xt = warm_start_reference * fwd_alpha + fwd_sigma * torch.randn_like(warm_start_reference)
        else:
            diffusion_steps = self.diffusion_steps
            xt = torch.randn_like(prior) * temperature
        xt = xt * (1.0 - self.fix_mask) + prior * self.fix_mask
        if preserve_history:
            log["sample_history"][:, 0] = xt.cpu().numpy()

        with torch.set_grad_enabled(requires_grad):
            condition_vec_cfg = model["condition"](condition_cfg, mask_cfg) if condition_cfg is not None else None
            condition_vec_cg = condition_cg

        # ===================== Sampling Schedule ====================
        if isinstance(sample_step_schedule, str):
            if sample_step_schedule in SUPPORTED_SAMPLING_STEP_SCHEDULE.keys():
                sample_step_schedule = SUPPORTED_SAMPLING_STEP_SCHEDULE[sample_step_schedule](
                    diffusion_steps, sample_steps
                )
            else:
                raise ValueError(f"Sampling step schedule {sample_step_schedule} is not supported.")
        elif callable(sample_step_schedule):
            sample_step_schedule = sample_step_schedule(diffusion_steps, sample_steps)
        else:
            raise ValueError("sample_step_schedule must be a callable or a string")

        alphas = self.alpha[sample_step_schedule]
        sigmas = self.sigma[sample_step_schedule]
        logSNRs = self.logSNR[sample_step_schedule]
        hs = torch.zeros_like(logSNRs)
        hs[1:] = logSNRs[:-1] - logSNRs[1:]  # hs[0] is not correctly calculated, but it will not be used.
        stds = torch.zeros((sample_steps + 1,), device=self.device)
        stds[1:] = sigmas[:-1] / sigmas[1:] * (1 - (alphas[1:] / alphas[:-1]) ** 2).sqrt()

        buffer = []

        # ===================== Denoising Loop ========================
        loop_steps = [1] * diffusion_x_sampling_steps + list(range(1, sample_steps + 1))
        for i in reversed(loop_steps):
            t = torch.full((n_samples,), sample_step_schedule[i], dtype=torch.long, device=self.device)

            # guided sampling
            pred, logp = self.guided_sampling(
                xt, t, alphas[i], sigmas[i], model, condition_vec_cfg, w_cfg, condition_vec_cg, w_cg, requires_grad
            )

            # clip the prediction
            pred = self.clip_prediction(pred, xt, alphas[i], sigmas[i])

            # noise & data prediction
            eps_theta = pred if self.predict_noise else xtheta_to_epstheta(xt, alphas[i], sigmas[i], pred)
            x_theta = pred if not self.predict_noise else epstheta_to_xtheta(xt, alphas[i], sigmas[i], pred)

            # one-step update
            if solver == "ddpm":
                xt = (alphas[i - 1] / alphas[i]) * (xt - sigmas[i] * eps_theta) + (
                    sigmas[i - 1] ** 2 - stds[i] ** 2 + 1e-8
                ).sqrt() * eps_theta
                if i > 1:
                    xt += stds[i] * torch.randn_like(xt)

            elif solver == "ddim":
                xt = alphas[i - 1] * ((xt - sigmas[i] * eps_theta) / alphas[i]) + sigmas[i - 1] * eps_theta

            elif solver == "ode_dpmsolver_1":
                xt = (alphas[i - 1] / alphas[i]) * xt - sigmas[i - 1] * torch.expm1(hs[i]) * eps_theta

            elif solver == "ode_dpmsolver++_1":
                xt = (sigmas[i - 1] / sigmas[i]) * xt - alphas[i - 1] * torch.expm1(-hs[i]) * x_theta

            elif solver == "ode_dpmsolver++_2M":
                buffer.append(x_theta)
                if i < sample_steps:
                    r = hs[i + 1] / hs[i]
                    D = (1 + 0.5 / r) * buffer[-1] - 0.5 / r * buffer[-2]
                    xt = (sigmas[i - 1] / sigmas[i]) * xt - alphas[i - 1] * torch.expm1(-hs[i]) * D
                else:
                    xt = (sigmas[i - 1] / sigmas[i]) * xt - alphas[i - 1] * torch.expm1(-hs[i]) * x_theta

            elif solver == "sde_dpmsolver_1":
                xt = (
                    (alphas[i - 1] / alphas[i]) * xt
                    - 2 * sigmas[i - 1] * torch.expm1(hs[i]) * eps_theta
                    + sigmas[i - 1] * torch.expm1(2 * hs[i]).sqrt() * torch.randn_like(xt)
                )

            elif solver == "sde_dpmsolver++_1":
                xt = (
                    (sigmas[i - 1] / sigmas[i]) * (-hs[i]).exp() * xt
                    - alphas[i - 1] * torch.expm1(-2 * hs[i]) * x_theta
                    + sigmas[i - 1] * (-torch.expm1(-2 * hs[i])).sqrt() * torch.randn_like(xt)
                )

            elif solver == "sde_dpmsolver++_2M":
                buffer.append(x_theta)
                if i < sample_steps:
                    r = hs[i + 1] / hs[i]
                    D = (1 + 0.5 / r) * buffer[-1] - 0.5 / r * buffer[-2]
                    xt = (
                        (sigmas[i - 1] / sigmas[i]) * (-hs[i]).exp() * xt
                        - alphas[i - 1] * torch.expm1(-2 * hs[i]) * D
                        + sigmas[i - 1] * (-torch.expm1(-2 * hs[i])).sqrt() * torch.randn_like(xt)
                    )
                else:
                    xt = (
                        (sigmas[i - 1] / sigmas[i]) * (-hs[i]).exp() * xt
                        - alphas[i - 1] * torch.expm1(-2 * hs[i]) * x_theta
                        + sigmas[i - 1] * (-torch.expm1(-2 * hs[i])).sqrt() * torch.randn_like(xt)
                    )

            # fix the known portion, and preserve the sampling history
            xt = xt * (1.0 - self.fix_mask) + prior * self.fix_mask
            if preserve_history:
                log["sample_history"][:, sample_steps - i + 1] = xt.cpu().numpy()

        # ================= Post-processing =================
        if self.classifier is not None:
            with torch.no_grad():
                t = torch.zeros((n_samples,), dtype=torch.long, device=self.device)
                logp = self.classifier.logp(xt, t, condition_vec_cg)
            log["log_p"] = logp

        if self.clip_pred:
            xt = xt.clip(self.x_min, self.x_max)

        return xt, log


class ContinuousDiffusionSDE(BaseDiffusionSDE):
    """Continuous-time Diffusion SDE (VP-SDE)

    The Diffusion SDE is currently one of the most commonly used formulations of diffusion processes.
    Its training process involves utilizing neural networks to estimate its scaled score function,
    which is used to compute the reverse process. The Diffusion SDE has reverse processes
    in both SDE and ODE forms, sharing the same marginal distribution.
    The first-order discretized forms of both are equivalent to well-known models such as DDPM and DDIM.
    DPM-Solvers have observed the semi-linearity of the reverse process and have computed its exact solution.

    The ContinuousDiffusionSDE is the continuous-time version of the Diffusion SDE.
    It estimates the score function at any $t\in[0,T]$ and solves the reverse process in continuous time.
    The sampling steps are required to be greater than 1.

    Args:
    - nn_diffusion: BaseNNDiffusion
        The neural network backbone for the Diffusion model.
    - nn_condition: Optional[BaseNNCondition]
        The neural network backbone for the condition embedding.

    - fix_mask: Union[list, np.ndarray, torch.Tensor]
        Fix some portion of the input data, and only allow the diffusion model to complete the rest part.
        The mask should be in the shape of `x_shape`.
    - loss_weight: Union[list, np.ndarray, torch.Tensor]
        Add loss weight. The weight should be in the shape of `x_shape`.

    - classifier: Optional[BaseClassifier]
        Add a classifier to enable classifier-guidance.

    - grad_clip_norm: Optional[float]
        Gradient clipping norm.
    - ema_rate: float
        Exponential moving average rate.
    - optim_params: Optional[dict]
        Optimizer parameters.

    - epsilon: float
        The minimum time step for the diffusion reverse process.
        In practice, using a very small value instead of `0` can avoid numerical instability.

    - noise_schedule: Union[str, Dict[str, Callable]]
        The noise schedule for the diffusion process. Can be "linear" or "cosine".
    - noise_schedule_params: Optional[dict]
        The parameters for the noise schedule.

    - x_max: Optional[torch.Tensor]
        The maximum value for the input data. `None` indicates no constraint.
    - x_min: Optional[torch.Tensor]
        The minimum value for the input data. `None` indicates no constraint.

    - predict_noise: bool
        Whether to predict the noise or the data.

    - device: Union[torch.device, str]
        The device to run the model.
    """

    def __init__(
        self,
        # ----------------- Neural Networks ----------------- #
        nn_diffusion: BaseNNDiffusion,
        nn_condition: Optional[BaseNNCondition] = None,
        # ----------------- Masks ----------------- #
        fix_mask: Optional[torch.Tensor] = None,  # be in the shape of `x_shape`
        loss_weight: Optional[torch.Tensor] = None,  # be in the shape of `x_shape`
        # ------------------ Plugins ---------------- #
        # Add a classifier to enable classifier-guidance
        classifier: Optional[BaseClassifier] = None,
        # ------------------ Training Params ---------------- #
        ema_rate: float = 0.995,
        # ------------------- Diffusion Params ------------------- #
        epsilon: float = 1e-3,
        x_max: Optional[torch.Tensor] = None,
        x_min: Optional[torch.Tensor] = None,
        predict_noise: bool = True,
        noise_schedule: Union[str, Dict[str, Callable]] = "cosine",
        noise_schedule_params: Optional[dict] = None,
    ):
        super().__init__(
            nn_diffusion,
            nn_condition,
            fix_mask,
            loss_weight,
            classifier,
            ema_rate,
            epsilon,
            x_max,
            x_min,
            predict_noise,
        )

        self.save_hyperparameters(ignore=["nn_diffusion", "nn_condition", "classifier"])

        # ==================== Continuous Time-step Range ====================
        self.t_diffusion = [epsilon, 1.0]
        if noise_schedule == "cosine":
            self.t_diffusion[1] = 0.9946

        # ===================== Noise Schedule ======================
        if isinstance(noise_schedule, str):
            if noise_schedule in SUPPORTED_NOISE_SCHEDULES.keys():
                self.noise_schedule_funcs = SUPPORTED_NOISE_SCHEDULES[noise_schedule]
                self.noise_schedule_params = noise_schedule_params
            else:
                raise ValueError(f"Noise schedule {noise_schedule} is not supported.")
        elif isinstance(noise_schedule, dict):
            self.noise_schedule_funcs = noise_schedule
            self.noise_schedule_params = noise_schedule_params
        else:
            raise ValueError("noise_schedule must be a callable or a string")

    # ==================== Training: Score Matching ======================

    def add_noise(self, x0: torch.Tensor, t: Optional[torch.Tensor] = None, eps: Optional[torch.Tensor] = None):
        """Forward process of the diffusion model.

        Args:
            x0: torch.Tensor,
                Samples from the target distribution. shape: (batch_size, *x_shape)
            t: torch.Tensor,
                Discrete time steps for the diffusion process. shape: (batch_size,).
                If `None`, randomly sample from Uniform(0, T).
            eps: torch.Tensor,
                Noise for the diffusion process. shape: (batch_size, *x_shape).
                If `None`, randomly sample from Normal(0, I).

        Returns:
            xt: torch.Tensor,
                The noisy samples. shape: (batch_size, *x_shape).
            t: torch.Tensor,
                The discrete time steps. shape: (batch_size,).
            eps: torch.Tensor,
                The noise. shape: (batch_size, *x_shape).
        """
        t = t or (
            torch.rand((x0.shape[0],), device=self.device) * (self.t_diffusion[1] - self.t_diffusion[0])
            + self.t_diffusion[0]
        )
        eps = eps or torch.randn_like(x0)

        alpha, sigma = self.noise_schedule_funcs["forward"](t, **(self.noise_schedule_params or {}))
        alpha, sigma = at_least_ndim(alpha, x0.dim()), at_least_ndim(sigma, x0.dim())

        xt = alpha * x0 + sigma * eps
        xt = (1.0 - self.fix_mask) * xt + self.fix_mask * x0

        return xt, t, eps

    # ==================== Sampling: Solving SDE/ODE ======================

    def sample(
        self,
        # ---------- the known fixed portion ---------- #
        prior: torch.Tensor,
        # ----------------- sampling ----------------- #
        solver: str = "ddpm",
        n_samples: int = 1,
        sample_steps: int = 5,
        sample_step_schedule: Union[str, Callable] = "uniform_continuous",
        use_ema: bool = True,
        temperature: float = 1.0,
        # ------------------ guidance ------------------ #
        condition_cfg=None,
        mask_cfg=None,
        w_cfg: float = 0.0,
        condition_cg=None,
        w_cg: float = 0.0,
        # ----------- Diffusion-X sampling ----------
        diffusion_x_sampling_steps: int = 0,
        # ----------- Warm-Starting -----------
        warm_start_reference: Optional[torch.Tensor] = None,
        warm_start_forward_level: float = 0.3,
        # ------------------ others ------------------ #
        requires_grad: bool = False,
        preserve_history: bool = False,
        **kwargs,
    ):
        """Sampling.

        Inputs:
        - prior: torch.Tensor
            The known fixed portion of the input data. Should be in the shape of generated data.
            Use `torch.zeros((n_samples, *x_shape))` for non-prior sampling.

        - solver: str
            The solver for the reverse process. Check `supported_solvers` property for available solvers.
        - n_samples: int
            The number of samples to generate.
        - sample_steps: int
            The number of sampling steps. Should be greater than 1.
        - sample_step_schedule: Union[str, Callable]
            The schedule for the sampling steps.
        - use_ema: bool
            Whether to use the exponential moving average model.
        - temperature: float
            The temperature for sampling.

        - condition_cfg: Optional
            Condition for Classifier-free-guidance.
        - mask_cfg: Optional
            Mask for Classifier-guidance.
        - w_cfg: float
            Weight for Classifier-free-guidance.
        - condition_cg: Optional
            Condition for Classifier-guidance.
        - w_cg: float
            Weight for Classifier-guidance.

        - diffusion_x_sampling_steps: int
            The number of diffusion steps for diffusion-x sampling.

        - warm_start_reference: Optional[torch.Tensor]
            Reference data for warm-starting sampling. `None` indicates no warm-starting.
        - warm_start_forward_level: float
            The forward noise level to perturb the reference data. Should be in the range of `[0., 1.]`, where `1` indicates pure noise.

        - requires_grad: bool
            Whether to preserve gradients.
        - preserve_history: bool
            Whether to preserve the sampling history.

        Outputs:
        - x0: torch.Tensor
            Generated samples. Be in the shape of `(n_samples, *x_shape)`.
        - log: dict
            The log dictionary.
        """
        assert solver in SUPPORTED_SOLVERS, f"Solver {solver} is not supported."

        # ===================== Initialization =====================
        log = {
            "sample_history": np.empty((n_samples, sample_steps + 1, *prior.shape)) if preserve_history else None,
        }

        model = self.model if not use_ema else self.model_ema

        prior = prior.to(self.device)
        if isinstance(warm_start_reference, torch.Tensor) and 0 < warm_start_forward_level < 1:
            warm_start_forward_level = self.t_diffusion[0] + warm_start_forward_level * (
                self.t_diffusion[1] - self.t_diffusion[0]
            )
            fwd_alpha, fwd_sigma = self.noise_schedule_funcs["forward"](
                torch.tensor([self.t_diffusion[1]], device=self.device) * warm_start_forward_level,
                **(self.noise_schedule_params or {}),
            )
            xt = warm_start_reference * fwd_alpha + fwd_sigma * torch.randn_like(warm_start_reference)
            t_diffusion = [self.t_diffusion[0], warm_start_forward_level]
        else:
            xt = torch.randn_like(prior) * temperature
            t_diffusion = self.t_diffusion
        xt = xt * (1.0 - self.fix_mask) + prior * self.fix_mask
        if preserve_history:
            log["sample_history"][:, 0] = xt.cpu().numpy()

        with torch.set_grad_enabled(requires_grad):
            condition_vec_cfg = model["condition"](condition_cfg, mask_cfg) if condition_cfg is not None else None
            condition_vec_cg = condition_cg

        # ===================== Sampling Schedule ====================
        if isinstance(sample_step_schedule, str):
            if sample_step_schedule in SUPPORTED_SAMPLING_STEP_SCHEDULE.keys():
                sample_step_schedule = SUPPORTED_SAMPLING_STEP_SCHEDULE[sample_step_schedule](t_diffusion, sample_steps)
            else:
                raise ValueError(f"Sampling step schedule {sample_step_schedule} is not supported.")
        elif callable(sample_step_schedule):
            sample_step_schedule = sample_step_schedule(t_diffusion, sample_steps)
        else:
            raise ValueError("sample_step_schedule must be a callable or a string")

        alphas, sigmas = self.noise_schedule_funcs["forward"](
            sample_step_schedule, **(self.noise_schedule_params or {})
        )
        logSNRs = torch.log(alphas / sigmas)
        hs = torch.zeros_like(logSNRs)
        hs[1:] = logSNRs[:-1] - logSNRs[1:]  # hs[0] is not correctly calculated, but it will not be used.
        stds = torch.zeros((sample_steps + 1,), device=self.device)
        stds[1:] = sigmas[:-1] / sigmas[1:] * (1 - (alphas[1:] / alphas[:-1]) ** 2).sqrt()

        buffer = []

        # ===================== Denoising Loop ========================
        loop_steps = [1] * diffusion_x_sampling_steps + list(range(1, sample_steps + 1))
        for i in reversed(loop_steps):
            t = torch.full((n_samples,), sample_step_schedule[i], device=self.device)

            # guided sampling
            pred, logp = self.guided_sampling(
                xt, t, alphas[i], sigmas[i], model, condition_vec_cfg, w_cfg, condition_vec_cg, w_cg, requires_grad
            )

            # clip the prediction
            pred = self.clip_prediction(pred, xt, alphas[i], sigmas[i])

            # transform to eps_theta
            eps_theta = pred if self.predict_noise else xtheta_to_epstheta(xt, alphas[i], sigmas[i], pred)
            x_theta = pred if not self.predict_noise else epstheta_to_xtheta(xt, alphas[i], sigmas[i], pred)

            # one-step update
            if solver == "ddpm":
                xt = (alphas[i - 1] / alphas[i]) * (xt - sigmas[i] * eps_theta) + (
                    sigmas[i - 1] ** 2 - stds[i] ** 2 + 1e-8
                ).sqrt() * eps_theta
                if i > 1:
                    xt += stds[i] * torch.randn_like(xt)

            elif solver == "ddim":
                xt = alphas[i - 1] * ((xt - sigmas[i] * eps_theta) / alphas[i]) + sigmas[i - 1] * eps_theta

            elif solver == "ode_dpmsolver_1":
                xt = (alphas[i - 1] / alphas[i]) * xt - sigmas[i - 1] * torch.expm1(hs[i]) * eps_theta

            elif solver == "ode_dpmsolver++_1":
                xt = (sigmas[i - 1] / sigmas[i]) * xt - alphas[i - 1] * torch.expm1(-hs[i]) * x_theta

            elif solver == "ode_dpmsolver++_2M":
                buffer.append(x_theta)
                if i < sample_steps:
                    r = hs[i + 1] / hs[i]
                    D = (1 + 0.5 / r) * buffer[-1] - 0.5 / r * buffer[-2]
                    xt = (sigmas[i - 1] / sigmas[i]) * xt - alphas[i - 1] * torch.expm1(-hs[i]) * D
                else:
                    xt = (sigmas[i - 1] / sigmas[i]) * xt - alphas[i - 1] * torch.expm1(-hs[i]) * x_theta

            elif solver == "sde_dpmsolver_1":
                xt = (
                    (alphas[i - 1] / alphas[i]) * xt
                    - 2 * sigmas[i - 1] * torch.expm1(hs[i]) * eps_theta
                    + sigmas[i - 1] * torch.expm1(2 * hs[i]).sqrt() * torch.randn_like(xt)
                )

            elif solver == "sde_dpmsolver++_1":
                xt = (
                    (sigmas[i - 1] / sigmas[i]) * (-hs[i]).exp() * xt
                    - alphas[i - 1] * torch.expm1(-2 * hs[i]) * x_theta
                    + sigmas[i - 1] * (-torch.expm1(-2 * hs[i])).sqrt() * torch.randn_like(xt)
                )

            elif solver == "sde_dpmsolver++_2M":
                buffer.append(x_theta)
                if i < sample_steps:
                    r = hs[i + 1] / hs[i]
                    D = (1 + 0.5 / r) * buffer[-1] - 0.5 / r * buffer[-2]
                    xt = (
                        (sigmas[i - 1] / sigmas[i]) * (-hs[i]).exp() * xt
                        - alphas[i - 1] * torch.expm1(-2 * hs[i]) * D
                        + sigmas[i - 1] * (-torch.expm1(-2 * hs[i])).sqrt() * torch.randn_like(xt)
                    )
                else:
                    xt = (
                        (sigmas[i - 1] / sigmas[i]) * (-hs[i]).exp() * xt
                        - alphas[i - 1] * torch.expm1(-2 * hs[i]) * x_theta
                        + sigmas[i - 1] * (-torch.expm1(-2 * hs[i])).sqrt() * torch.randn_like(xt)
                    )

            # fix the known portion, and preserve the sampling history
            xt = xt * (1.0 - self.fix_mask) + prior * self.fix_mask
            if preserve_history:
                log["sample_history"][:, sample_steps - i + 1] = xt.cpu().numpy()

        # ================= Post-processing =================
        if self.classifier is not None and w_cg != 0.0:
            with torch.no_grad():
                t = torch.zeros((n_samples,), dtype=torch.long, device=self.device)
                logp = self.classifier.logp(xt, t, condition_vec_cg)
            log["log_p"] = logp

        if self.clip_pred:
            xt = xt.clip(self.x_min, self.x_max)

        return xt, log