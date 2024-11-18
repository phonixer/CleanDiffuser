# $ python3
# ! export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib/nvidia
# ! export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/home/zrg/.mujoco/mujoco210/bin
import mujoco_py
import os
mj_path = mujoco_py.utils.discover_mujoco()
xml_path = os.path.join(mj_path, 'model', 'humanoid.xml')
model = mujoco_py.load_model_from_path(xml_path)
sim = mujoco_py.MjSim(model)

print(sim.data.qpos)