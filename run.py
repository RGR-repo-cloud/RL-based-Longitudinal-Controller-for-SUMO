#!/usr/bin/env python3
import datetime
from pathlib import Path
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import copy
import math
import os
import sys
import time
import pickle as pkl

from video import VideoRecorder
from logger import Logger
from replay_buffer import ReplayBuffer
import utils
import hydra
from agent_system import IndividualMultiAgent, SharedMultiAgent


class Workspace(object):
    def __init__(self, cfg):

        #change working directory to location of checkpoint or create new one
        if cfg.load_checkpoint:
            os.chdir(os.path.join(os.getcwd(), cfg.checkpoint_dir))
        else:
            dir = os.path.join(os.getcwd(), datetime.datetime.now().strftime('%Y-%m-%d'), datetime.datetime.now().strftime('%H-%M'))
            Path(dir).mkdir(parents=True, exist_ok=True)
            os.chdir(dir)
            
        self.work_dir = os.getcwd()
        print(f'workspace: {self.work_dir}')

        self.cfg = cfg

        utils.set_seed_everywhere(cfg.seed)
        self.device = torch.device(cfg.device)
        
        #register environment
        self.env = utils.import_flow_env(env_name=self.cfg.env, render=self.cfg.render, evaluate=False)
        self.env.seed(self.cfg.seed)
        self.agent_ids = self.env.agents

        #initialize loggers
        self.loggers = {}
        for agent in self.agent_ids:
            self.loggers[agent] = Logger(   self.work_dir,
                                            agent_id=agent,
                                            save_tb=cfg.log_save_tb,
                                            log_frequency=cfg.log_frequency,
                                            agent=self.cfg.agent.name,
                                            file_exists=cfg.load_checkpoint)
        
        #initialize agents
        if self.cfg.multi_agent_mode == 'individual':
            
            #initialize input and output parameters
            obs_spaces, act_spaces, act_ranges = {}, {}, {}
            for agent in self.agent_ids:
                obs_spaces[agent] = self.env.observation_space[agent].shape
                act_spaces[agent] = self.env.action_space[agent].shape
                act_ranges[agent] = [   float(self.env.action_space[agent].low.min()),
                                        float(self.env.action_space[agent].high.max())]
                
            self.multi_agent = IndividualMultiAgent(self.cfg, self.agent_ids, obs_spaces, act_spaces, act_ranges, int(self.cfg.replay_buffer_capacity), self.device, self.cfg.mode, self.cfg.agent)
        
        elif self.cfg.multi_agent_mode == 'shared':
            
            #initialize input and output parameters of the first agent_id (all agents should be the same)
            obs_space = self.env.observation_space[self.agent_ids[0]].shape
            act_space = self.env.action_space[self.agent_ids[0]].shape
            act_range = [   float(self.env.action_space[self.agent_ids[0]].low.min()),
                            float(self.env.action_space[self.agent_ids[0]].high.max())]
                
            self.multi_agent = SharedMultiAgent(self.cfg, self.agent_ids, obs_space, act_space, act_range, int(self.cfg.replay_buffer_capacity)*len(self.agent_ids), self.device, self.cfg.mode, self.cfg.agent)
  
        else:
            raise Exception('no valid multiagent_mode')

        self.step = 0
        
        #load checkpoint
        if cfg.load_checkpoint:
            self.step = self.multi_agent.load_checkpoint(os.path.join(os.getcwd(), 'checkpoints'), self.cfg.checkpoint_name)


        self.video_recorder = VideoRecorder(
            self.work_dir if cfg.save_video else None)
        


    def evaluate(self):
        average_episode_rewards = {}
        for agent in self.agent_ids:
            average_episode_rewards[agent] = 0
        for episode in range(self.cfg.num_eval_episodes):
            obs = self.env.reset()
            self.multi_agent.reset()
            self.video_recorder.init(enabled=(episode == 0))
            done = False
            episode_rewards = {}
            for agent in self.agent_ids:
                    episode_rewards[agent] = 0
            while not done:
                actions = self.multi_agent.act(obs, sample=False, mode="eval")
                obs, rewards, done, _ = self.env.step(actions)
                self.video_recorder.record(self.env)
                for agent in self.agent_ids:
                    episode_rewards[agent] += rewards[agent]

            for agent in self.agent_ids:
                average_episode_rewards[agent] += episode_rewards[agent]
            self.video_recorder.save(f'{self.step}.mp4')
        for agent in self.agent_ids:
            average_episode_rewards[agent] /= self.cfg.num_eval_episodes
            self.loggers[agent].log('eval/episode_reward', average_episode_rewards[agent],
                        self.step)
            self.loggers[agent].dump(self.step)


    def train(self):
        episode_rewards, done, episode_step = {}, False, 0
        episode = self.step / self.env.horizon
        for agent in self.agent_ids:
            episode_rewards[agent] = 0
        obs = self.env.reset()
        self.multi_agent.reset()
        
        while self.step < self.cfg.num_train_steps:
            start_time = time.time()

            # sample action for data collection
            if self.step < self.cfg.num_seed_steps:
                actions = {}
                for agent in self.agent_ids:
                    actions[agent] = self.env.action_space[agent].sample()
            else:
                actions = self.multi_agent.act(obs, sample=True, mode="eval")

            # run training update
            if self.step >= self.cfg.num_seed_steps:
                self.multi_agent.update(self.loggers, self.step)

            next_obs, rewards, done, _ = self.env.step(actions)

            # allow infinite bootstrap
            done = float(done)
            done_no_max = 0 if episode_step + 1 == self.env.horizon else done
            for agent in self.agent_ids:
                episode_rewards[agent] += rewards[agent]
            self.multi_agent.add_to_buffer(obs, actions, rewards, next_obs, done, done_no_max)

            obs = next_obs
            episode_step += 1
            self.step += 1


            if done:
                
                for agent in self.agent_ids:
                    self.loggers[agent].log('train/duration',
                                        time.time() - start_time, self.step)
                    self.loggers[agent].log('train/episode', episode, self.step)
                    self.loggers[agent].log('train/episode_reward', episode_rewards[agent],
                                           self.step)
                    self.loggers[agent].dump(
                                        self.step, save=(self.step > self.cfg.num_seed_steps))
                    
                # evaluate agent periodically
                if self.step > 0 and self.step % self.cfg.eval_frequency == 0:
                    for agent in self.agent_ids:
                        self.loggers[agent].log('eval/episode', episode, self.step)
                    self.evaluate()
                    
                obs = self.env.reset()
                self.multi_agent.reset()
                done = False
                for agent in self.agent_ids:
                    episode_rewards[agent] = 0
                episode_step = 0
                episode += 1

        
        #save models and optimizers
        if self.cfg.save_checkpoint:
            self.multi_agent.save_checkpoint(os.path.join(os.getcwd(), 'checkpoints'), self.step)
            

            




@hydra.main(config_path='config/run.yaml', strict=True)
def main(cfg):
    workspace = Workspace(cfg)
    
    if cfg.mode == 'train':
        start = time.time()
        workspace.train()
        end = time.time()
        print('TOTAL_TIME:')
        print(end-start)
    elif cfg.mode == 'eval':
        workspace.evaluate()
    else:
        raise Exception('no valid running mode')


if __name__ == '__main__':
    main()