import os

import d4rl
import gym
import hydra
import numpy as np
import torch
from tqdm import tqdm
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader

from cleandiffuser.classifier import CumRewClassifier
from cleandiffuser.dataset.d4rl_maze2d_dataset import D4RLMaze2DDataset
from cleandiffuser.dataset.dataset_utils import loop_dataloader
from cleandiffuser.diffusion import DiscreteDiffusionSDE
from cleandiffuser.nn_classifier import HalfJannerUNet1d
from cleandiffuser.nn_diffusion import JannerUNet1d
from cleandiffuser.utils import report_parameters
from utils import set_seed


@hydra.main(config_path="../configs/diffuser/maze2d", config_name="maze2d", version_base=None)
def pipeline(args):

    set_seed(args.seed)

    save_path = f'results/{args.pipeline_name}/{args.task.env_name}/'
    if os.path.exists(save_path) is False:
        os.makedirs(save_path)

    # ---------------------- Create Dataset ----------------------
    env = gym.make(args.task.env_name)
    dataset = D4RLMaze2DDataset(
        env.get_dataset(), horizon=args.task.horizon, discount=args.discount,
        noreaching_penalty=args.noreaching_penalty,)
    dataloader = DataLoader(
        dataset, batch_size=args.batch_size, shuffle=True, num_workers=4, pin_memory=True, drop_last=True)
    obs_dim, act_dim = dataset.o_dim, dataset.a_dim

    # --------------- Network Architecture -----------------
    nn_diffusion = JannerUNet1d(
        obs_dim + act_dim, model_dim=args.model_dim, emb_dim=args.model_dim, dim_mult=args.task.dim_mult,
        timestep_emb_type="positional", attention=False, kernel_size=5)
    nn_classifier = HalfJannerUNet1d(
        args.task.horizon, obs_dim + act_dim, out_dim=1,
        model_dim=args.model_dim, emb_dim=args.model_dim, dim_mult=args.task.dim_mult,
        timestep_emb_type="positional", kernel_size=3)

    print(f"======================= Parameter Report of Diffusion Model =======================")
    report_parameters(nn_diffusion)
    print(f"======================= Parameter Report of Classifier =======================")
    report_parameters(nn_classifier)
    print(f"==============================================================================")

    # --------------- Classifier Guidance --------------------
    classifier = CumRewClassifier(nn_classifier, device=args.device)

    # ----------------- Masking -------------------
    fix_mask = torch.zeros((args.task.horizon, obs_dim + act_dim))
    fix_mask[0, :obs_dim] = 1.
    fix_mask[-1, :obs_dim] = 1. # target
    loss_weight = torch.ones((args.task.horizon, obs_dim + act_dim))
    loss_weight[0, obs_dim:] = args.action_loss_weight

    # --------------- Diffusion Model --------------------
    agent = DiscreteDiffusionSDE(
        nn_diffusion, None,
        fix_mask=fix_mask, loss_weight=loss_weight, classifier=classifier, ema_rate=args.ema_rate,
        device=args.device, diffusion_steps=args.diffusion_steps, predict_noise=args.predict_noise)

    # ---------------------- Training ----------------------
    if args.mode == "train":

        diffusion_lr_scheduler = CosineAnnealingLR(agent.optimizer, args.diffusion_gradient_steps)
        classifier_lr_scheduler = CosineAnnealingLR(agent.classifier.optim, args.classifier_gradient_steps)

        agent.train()

        n_gradient_step = 0
        log = {"avg_loss_diffusion": 0., "avg_loss_classifier": 0.}

        pbar = tqdm(total=args.diffusion_gradient_steps)
        for batch in loop_dataloader(dataloader):

            obs = batch["obs"]["state"].to(args.device)
            act = batch["act"].to(args.device)
            val = batch["val"].to(args.device)

            x = torch.cat([obs, act], -1)

            # ----------- Gradient Step ------------
            log["avg_loss_diffusion"] += agent.update(x)['loss']
            diffusion_lr_scheduler.step()
            if n_gradient_step <= args.classifier_gradient_steps:
                log["avg_loss_classifier"] += agent.update_classifier(x, val)['loss']
                classifier_lr_scheduler.step()

            # ----------- Logging ------------
            if (n_gradient_step + 1) % args.log_interval == 0:
                log["gradient_steps"] = n_gradient_step + 1
                log["avg_loss_diffusion"] /= args.log_interval
                log["avg_loss_classifier"] /= args.log_interval
                print(log)
                log = {"avg_loss_diffusion": 0., "avg_loss_classifier": 0.}

            # ----------- Saving ------------
            if (n_gradient_step + 1) % args.save_interval == 0:
                agent.save(save_path + f"diffusion_ckpt_{n_gradient_step + 1}.pt")
                agent.classifier.save(save_path + f"classifier_ckpt_{n_gradient_step + 1}.pt")
                agent.save(save_path + f"diffusion_ckpt_latest.pt")
                agent.classifier.save(save_path + f"classifier_ckpt_latest.pt")

            n_gradient_step += 1
            if n_gradient_step >= args.diffusion_gradient_steps:
                break

            pbar.update(1)

        pbar.close()

    # ---------------------- Inference ----------------------
    elif args.mode == "inference":

        agent.load(save_path + f"diffusion_ckpt_{args.ckpt}.pt")
        agent.classifier.load(save_path + f"classifier_ckpt_{args.ckpt}.pt")

        agent.eval()

        # env_eval = gym.vector.make(args.task.env_name, args.num_envs)
        env_eval = [gym.make(args.task.env_name) for _ in range(args.num_envs)]
        normalizer = dataset.get_normalizer()
        episode_rewards = []

        prior = torch.zeros((args.num_envs, args.task.horizon, obs_dim + act_dim), device=args.device)
        for i in range(args.num_episodes):

            # reset the environment
            fix_target = (0.5, 0.5)

            [env.set_target(fix_target) for env in env_eval]
            obs, ep_reward, cum_done, t = [env.reset() for env in env_eval], 0., 0., 0
            target = [[*env.get_target(), 0, 0] for env in env_eval]
            
            logs = {'obs': [], 'act': [], 'rew': [], 'logp': [], 'target': target}
            print('target:', target)

            target = torch.tensor([normalizer.normalize(t) for t in target], device=args.device)
            

            while not np.all(cum_done) and t < 1000 + 1:
                # normalize obs
                obs = torch.tensor(normalizer.normalize(obs), device=args.device, dtype=torch.float32)

                # sample trajectories
                prior[:, 0, :obs_dim] = obs
                prior[:, -1, :obs_dim] = target
                traj, log = agent.sample(
                    prior.repeat(args.num_candidates, 1, 1),
                    solver=args.solver,
                    n_samples=args.num_candidates * args.num_envs,
                    sample_steps=args.sampling_steps,
                    use_ema=args.use_ema, w_cg=args.task.w_cg, temperature=args.temperature)

                # select the best plan
                logp = log["log_p"].view(args.num_candidates, args.num_envs, -1).sum(-1)
                idx = logp.argmax(0)
                act = traj.view(args.num_candidates, args.num_envs, args.task.horizon, -1)[
                      idx, torch.arange(args.num_envs), 0, obs_dim:]
                act = act.clip(-1., 1.).cpu().numpy()

                # logging
                
                traj = traj.view(args.num_candidates, args.num_envs, args.task.horizon, -1).cpu().numpy()
                traj_obs = traj[:, :, :, :obs_dim].reshape(-1, obs_dim) 
                obs = normalizer.unnormalize(traj_obs).reshape(args.num_candidates, args.num_envs, args.task.horizon, obs_dim)
                # obs = traj_obs.reshape(args.num_candidates, args.num_envs, args.task.horizon, obs_dim)
                acts = traj[:, :, :, obs_dim:]
                logs['obs'].append(obs)
                logs['act'].append(acts)
                logs['rew'].append(ep_reward)
                logs['logp'].append(logp.cpu().numpy())

                # save log
                # np.save(save_path + f"save_results_{args.ckpt}.npy", logs)

                # step
                # obs, rew, done, info = env_eval.step(act)
                results = [env.step(action) for env, action in zip(env_eval, act)]
                obs, rew, done, info = zip(*results)
                obs = np.array(obs)
                rew = np.array(rew)
                done = np.array(done)
                

                t += 1
                cum_done = done if cum_done is None else np.logical_or(cum_done, done)
                ep_reward += rew
                print(f'[t={t}] xy: {obs[:, :2]}')
                # print(f'[t={t}] cum_rew: {ep_reward}, '
                #       f'logp: {logp[idx, torch.arange(args.num_envs)]}')


            # clip the reward to [0, 1] since the max cumulative reward is 1
            episode_rewards.append(np.clip(ep_reward, 0., 1.))

            # save log
            np.save(save_path + f"save_results_{args.ckpt}.npy", logs)

        episode_rewards = [list(map(lambda x: env.get_normalized_score(x), r)) for r in episode_rewards]
        episode_rewards = np.array(episode_rewards)
        print(np.mean(episode_rewards, -1), np.std(episode_rewards, -1))

        

    else:
        raise ValueError(f"Invalid mode: {args.mode}")


if __name__ == "__main__":
    pipeline()