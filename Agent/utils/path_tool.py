"""
为整个工程提供统一的绝对路径
"""
import os
def get_project_root()->str:#获取项目根目录
    current_file=os.path.abspath(__file__)#os.path.abspath：转成完整绝对路径
    curren_dir=os.path.dirname(current_file)
    # os.path.dirname去掉文件名，只留文件夹路径
    # 例子：
    # 文件：C:/a/b/c.py
    # 文件夹：C:/a/b
    project_root=os.path.dirname(curren_dir)
    return project_root
def get_abs_path(relative_path:str)->str:#获得绝对路径
    project_root=get_project_root()
    return os.path.join(project_root,relative_path)