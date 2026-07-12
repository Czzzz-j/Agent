from utils.config_handler import prompts_conf
from utils.path_tool import get_abs_path
from utils.logger_handler import logger
#加载 “报告生成” 用的提示词内容
def load_system_prompts():
    try:
        system_prompt_path=get_abs_path(prompts_conf["main_prompt_path"])
    except KeyError as e:
        logger.error(f"[load_system_prompts]在yaml配置项中没用main_prompt_path配置项")
        raise e
    try:
        return open(system_prompt_path,'r',encoding="utf-8").read()
    except Exception as e:
        logger.error(f"[load_system_prompts]解析提示词出错，{str(e)}")
        raise e

def load_rag_prompts():
    try:
        rag_prompts_path=get_abs_path(prompts_conf["reg_summarize_prompt_path"])
    except KeyError as e:
        logger.error(f"[load_rag_prompts]在yaml配置项中没用reg_summarize_prompt_path配置项")
        raise e
    try:
        return open(rag_prompts_path,'r',encoding="utf-8").read()
    except Exception as e:
        logger.error(f"[load_rag_prompts]解析RAG总结提示词出错，{str(e)}")
        raise e

def load_report_prompts():
    try:
        report_prompt_path=get_abs_path(prompts_conf["report_prompt_path"])
    except KeyError as e:
        logger.error(f"[load_report_prompts]在yaml配置项中没用report_prompt_path配置项")
        raise e
    try:
        return open(report_prompt_path,"r",encoding="utf-8").read()
    except Exception as e:
        logger.error(f"[load_report_prompts]解析报告生成总结提示词出错，{str(e)}")
        raise e
if __name__ == "__main__":
    print(load_system_prompts())
