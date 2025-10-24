import sys
import os
import time
import re
import configparser


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.ini")

_config = None


def load_config(path: str = CONFIG_PATH):
	"""加载 ini 配置文件（单例缓存）"""
	global _config
	if _config is None:
		config = configparser.ConfigParser()
		config.read(path, encoding="utf-8")
		_config = config
	return _config

def get_config(section: str) -> dict:
	"""获取某个 section 的配置字典"""
	config = load_config()
	if section not in config:
		raise KeyError(f"配置文件中缺少 section [{section}]")
	return dict(config[section])

def save_token_to_config(app_token_data) -> None:
	"""把 token 写回 config.ini 的对应 section，并记录更新时间"""
	section="NIU-Account"
	cfg = load_config()
	if not cfg.has_section(section):
		cfg.add_section(section)
	token_data=app_token_data["data"]
	cfg.set(section, "access_token", token_data["access_token"])
	cfg.set(section, "refresh_token", token_data["refresh_token"])
	cfg.set(section, "refresh_token_expires_in", str(token_data["refresh_token_expires_in"]))
	cfg.set(section, "token_expires_in", str(token_data["token_expires_in"]))
	# 原地写回文件
	with open(CONFIG_PATH, "w", encoding="utf-8") as f:
		cfg.write(f)
	#print(f"[{section}] 保存缓存 token_data={token_data}")