import logging
from math import pi
import os
from pathlib import Path
import pickle
import pkgutil
import re
import sys
import time
from loguru import logger
import cv2
import gym
import gym.utils
import gym.utils.seeding
import hydra
import numpy as np
import pybullet as p
import pybullet_utils.bullet_client as bc

import calvin_env
from calvin_env.camera.camera import Camera
from calvin_env.utils.utils import FpsController, get_git_commit_hash

# A logger for this file
log = logging.getLogger(__name__)


class CalvinSimEnv(gym.Env):
    def __init__(
        self,
        robot_cfg,
        seed,
        use_vr,
        bullet_time_step,
        cameras,
        show_gui,
        scene_cfg,
        use_scene_info,
        use_egl,
        control_freq=30,
    ):
        self.p = p
        # for calculation of FPS
        self.t = time.time()
        self.prev_time = time.time()
        self.fps_controller = FpsController(bullet_time_step)
        self.use_vr = use_vr
        self.show_gui = show_gui
        self.use_scene_info = use_scene_info
        self.cid = -1
        self.ownsPhysicsClient = False
        self.use_egl = use_egl
        self.control_freq = control_freq
        self.action_repeat = int(bullet_time_step // control_freq)
        render_width = max([cameras[cam].width for cam in cameras]) if cameras else None
        render_height = max([cameras[cam].height for cam in cameras]) if cameras else None
        self.initialize_bullet(bullet_time_step, render_width, render_height)
        self.np_random = None
        self.seed(seed)
        self.robot = hydra.utils.instantiate(robot_cfg, cid=self.cid)
        self.scene = hydra.utils.instantiate(scene_cfg, p=self.p, cid=self.cid, np_random=self.np_random)

        # Load Env
        self.load()

        # init cameras after scene is loaded to have robot id available
        self.cameras: list[Camera] = [
            hydra.utils.instantiate(
                cameras[name], cid=self.cid, robot_id=self.robot.robot_uid, objects=self.scene.get_objects()
            )
            for name in cameras
        ]
        log.info(f"Using calvin_env with commit {get_git_commit_hash(Path(calvin_env.__file__))}.")

    def __del__(self):
        self.close()

    def reset(self, robot_obs=None, scene_obs=None):
        self.scene.reset(scene_obs)
        self.robot.reset(robot_obs)
        self.p.stepSimulation(physicsClientId=self.cid)
        return self.get_obs()

    # From pybullet gym_manipulator_envs code
    # https://github.com/bulletphysics/bullet3/blob/master/examples/pybullet/gym/pybullet_envs/gym_manipulator_envs.py
    def initialize_bullet(self, bullet_time_step, render_width, render_height):
        if self.cid < 0:
            self.ownsPhysicsClient = True
            if self.use_vr:
                self.p = bc.BulletClient(connection_mode=p.SHARED_MEMORY)
                cid = self.p._client
                if cid < 0:
                    log.error("Failed to connect to SHARED_MEMORY bullet server.\n" " Is it running?")
                    sys.exit(1)
                self.p.setRealTimeSimulation(enableRealTimeSimulation=1, physicsClientId=cid)
            elif self.show_gui:
                self.p = bc.BulletClient(connection_mode=p.GUI)
                cid = self.p._client
                if cid < 0:
                    log.error("Failed to connect to GUI.")
                self.p.resetDebugVisualizerCamera(
                    cameraDistance=1.5,  
                    cameraYaw=50,        
                    cameraPitch=-35,     
                    cameraTargetPosition=[0, 0, 0]
                )
            elif self.use_egl:
                options = f"--width={render_width} --height={render_height}"
                self.p = p
                cid = self.p.connect(p.DIRECT, options=options)
                p.configureDebugVisualizer(p.COV_ENABLE_GUI, 0, physicsClientId=cid)
                p.configureDebugVisualizer(p.COV_ENABLE_SEGMENTATION_MARK_PREVIEW, 0, physicsClientId=cid)
                p.configureDebugVisualizer(p.COV_ENABLE_DEPTH_BUFFER_PREVIEW, 0, physicsClientId=cid)
                p.configureDebugVisualizer(p.COV_ENABLE_RGB_BUFFER_PREVIEW, 0, physicsClientId=cid)
                egl = pkgutil.get_loader("eglRenderer")
                log.info("Loading EGL plugin (may segfault on misconfigured systems)...")
                if egl:
                    plugin = p.loadPlugin(egl.get_filename(), "_eglRendererPlugin")
                else:
                    plugin = p.loadPlugin("eglRendererPlugin")
                if plugin < 0:
                    log.error("\nPlugin Failed to load!\n")
                    sys.exit()
                # set environment variable for tacto renderer
                os.environ["PYOPENGL_PLATFORM"] = "egl"
                log.info("Successfully loaded egl plugin")
            else:
                self.p = bc.BulletClient(connection_mode=p.DIRECT)
                cid = self.p._client
                if cid < 0:
                    log.error("Failed to start DIRECT bullet mode.")
            log.info(f"Connected to server with id: {cid}")

            self.cid = cid
            self.p.resetSimulation(physicsClientId=self.cid)
            self.p.setPhysicsEngineParameter(deterministicOverlappingPairs=1, physicsClientId=self.cid)
            self.p.configureDebugVisualizer(self.p.COV_ENABLE_GUI, 0)
            log.info(f"Connected to server with id: {self.cid}")
            self.p.setTimeStep(1.0 / bullet_time_step, physicsClientId=self.cid)
            return cid

    def load(self):
        log.info("Resetting simulation")
        self.p.resetSimulation(physicsClientId=self.cid)
        log.info("Setting gravity")
        self.p.setGravity(0, 0, -9.8, physicsClientId=self.cid)

        self.robot.load()
        self.scene.load()

    def close(self):
        if self.ownsPhysicsClient:
            print("disconnecting id %d from server" % self.cid)
            if self.cid >= 0 and self.p is not None:
                try:
                    self.p.disconnect(physicsClientId=self.cid)
                except TypeError:
                    pass

        else:
            print("does not own physics client id")

    def render(self, mode="human"):
        """render is gym compatibility function"""
        rgb_obs, depth_obs, extr_obs, intr_obs, mask_obs = self.get_camera_obs()
        if mode == "human":
            if "rgb_static" in rgb_obs:
                img = rgb_obs["rgb_static"][:, :, ::-1]
                cv2.imshow("simulation cam", cv2.resize(img, (500, 500)))
            if "rgb_gripper" in rgb_obs:
                img2 = rgb_obs["rgb_gripper"][:, :, ::-1]
                cv2.imshow("gripper cam", cv2.resize(img2, (500, 500)))
            cv2.waitKey(1)
        elif mode == "rgb_array":
            assert "rgb_static" in rgb_obs, "Environment does not have static camera"
            return rgb_obs["rgb_static"]
        else:
            raise NotImplementedError

    def get_scene_info(self):
        return self.scene.get_info()

    def reset(self, robot_obs=None, scene_obs=None):
        self.scene.reset(scene_obs)
        self.robot.reset(robot_obs)
        self.p.stepSimulation(physicsClientId=self.cid)
        return self.get_obs() , self.get_info()

    def seed(self, seed=None):
        self.np_random, seed = gym.utils.seeding.np_random(seed)
        # self.robot.np_random = self.np_random  # use the same np_randomizer for robot as for env
        return [seed]

    def get_camera_obs(self):
        assert self.cameras is not None
        rgb_obs = {}
        depth_obs = {}
        extr_obs = {}
        intr_obs = {}
        mask_obs = {}
        for cam in self.cameras:
            rgb, depth = cam.render()
            rgb_obs[f"rgb_{cam.name}"] = rgb
            depth_obs[f"depth_{cam.name}"] = depth
            extr_obs[f"extr_{cam.name}"] = cam.get_extr()  # or cam.extr
            intr_obs[f"intr_{cam.name}"] = cam.get_intr()  # or cam.intr
            mask_obs[f"mask_{cam.name}"] = None#cam.get_mask()  # if available
        return rgb_obs, depth_obs, extr_obs, intr_obs, mask_obs

    def get_obs(self):
        """Collect camera, robot and scene observations."""
        rgb_obs, depth_obs, extr_obs, intr_obs, mask_obs = self.get_camera_obs()
        obs = {"rgb_obs": rgb_obs, "depth_obs": depth_obs, "extr_obs": extr_obs,
           "intr_obs": intr_obs,
           "mask_obs": mask_obs}
        obs.update(self.get_state_obs())

        return obs

    def get_state_obs(self):
        """
        Collect state observation dict
        --state_obs
            --robot_obs
                --robot_state_full
                    -- [tcp_pos, tcp_orn, gripper_opening_width]
                --gripper_opening_width
                --arm_joint_states
                --gripper_action}
            --scene_obs
        """
        robot_obs, robot_info = self.robot.get_observation()
        scene_obs = self.scene.get_obs()
        obs = {"robot_obs": robot_obs, "scene_obs": scene_obs, "robot_info": robot_info}
        return obs

    def get_info(self):
        _, robot_info = self.robot.get_observation()
        info = {"robot_info": robot_info}
        if self.use_scene_info:
            info["scene_info"] = self.scene.get_info()
        return info

    def step(self, action):
        self.robot.apply_action(action)
        for i in range(self.action_repeat):
            self.p.stepSimulation(physicsClientId=self.cid)
        self.scene.step()
        obs = self.get_obs()
        info = self.get_info()
        # obs, reward, done, info
        return obs, 0, False, info

    def reset_from_storage(self, filename):
        """
        Args:
            filename: file to load from.
        Returns:
            observation
        """
        with open(filename, "rb") as file:
            data = pickle.load(file)

        self.robot.reset_from_storage(data["robot"])
        self.scene.reset_from_storage(data["scene"])

        self.p.stepSimulation(physicsClientId=self.cid)

        return data["state_obs"], data["done"], data["info"]

    def serialize(self):
        data = {"time": time.time_ns() / (10**9), "robot": self.robot.serialize(), "scene": self.scene.serialize()}
        return data


def get_env(dataset_path, obs_space=None, show_gui=True, **kwargs):
    from pathlib import Path

    from omegaconf import OmegaConf

    render_conf = OmegaConf.load(Path(dataset_path) / ".hydra" / "merged_config.yaml")

    if obs_space is not None:
        exclude_keys = set(render_conf.cameras.keys()) - {
            re.split("_", key)[1] for key in obs_space["rgb_obs"] + obs_space["depth_obs"]
        }
        for k in exclude_keys:
            del render_conf.cameras[k]
    if "scene" in kwargs:
        scene_cfg = OmegaConf.load(Path(calvin_env.__file__).parents[1] / "conf/scene" / f"{kwargs['scene']}.yaml")
        render_conf.scene = scene_cfg
    if not hydra.core.global_hydra.GlobalHydra.instance().is_initialized():
        hydra.initialize(".")
    env = hydra.utils.instantiate(render_conf.env, show_gui=show_gui, use_vr=False, use_scene_info=True)
    return env


@hydra.main(config_path="../../conf", config_name="config_motion_data_collection")
def run_env(cfg):
    env = hydra.utils.instantiate(cfg.env, show_gui=True, use_vr=False, use_scene_info=True)

    env.reset()
    while True:
        action = {"action": np.array((0., 0, 0, 0, 0, 0, 1)),
                  "type": "cartesian_rel"}
        # cartesian actions can also be input directly as numpy arrays
        # action = np.array((0., 0, 0, 0, 0, 0, 1))

        # relative action in joint space
        # action = {"action": np.array((0., 0, 0, 0, 0, 0, 0, 1)),
        #           "type": "joint_rel"}

        env.step(action)
        # env.render()
        time.sleep(0.01)


def get_env_from_cfg():
    """Bypass Hydra's execution context and create the environment manually."""
    with hydra.initialize(config_path="../../conf"):
        cfg = hydra.compose(config_name="config_motion_data_collection")
        env = hydra.utils.instantiate(cfg.env, show_gui=False, use_vr=False, use_scene_info=True)
        assert env is not None, "Failed to create CustomSimEnv"
        return env
    
if __name__ == "__main__":
    run_env()
