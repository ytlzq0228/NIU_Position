import threading
import time
import requests
from config_ops import load_config,get_config,save_token_to_config
EARLY_REFRESH_SECONDS = 60  # 提前刷新窗口

def login_get_token(account,password):
	url = f"https://account.niu.com/v3/api/oauth2/token"
	
	try:
		data={
			"grant_type":"password",
			"scope":"base",
			"app_id":"niu_h8nv8eaz",
			"account":account,
			"password":password
		}
		resp = requests.post(url, data=data, timeout=10)
		resp.raise_for_status()  # 检查 HTTP 状态码
		token_data = resp.json()	   # 转换为 Python 字典
		if token_data.get("status")==0:
			return token_data
		else:
			print(token_data)
			return None
	except Exception as err:
		print(f"login_get_token API调用失败{err}")
		return None

def refresh_app_token(refresh_token):
	url = f"https://account.niu.com/v3/api/oauth2/token"
	
	try:
		data={
			"grant_type":"refresh_token",
			"scope":"base",
			"app_id":"niu_h8nv8eaz",
			"refresh_token":refresh_token
		}
		resp = requests.post(url, data=data, timeout=10)
		resp.raise_for_status()  # 检查 HTTP 状态码
		token_data = resp.json()	   # 转换为 Python 字典
		#print(token_data)
		if token_data.get("status")==0:
			return token_data
		else:
			print(token_data)
			return None
	except Exception as err:
		print(f"refresh_app_token API调用失败{err}")
		return None

def check_token_valid(app_token):
	url = f"https://app-api.niu.com/v5/scooter/list"
	
	try:
		headers={
			"token": app_token,
			"accept": "*/*",
			"content-type": "application/json",
			"accept-encoding": "br;q=1.0, gzip;q=0.9, deflate;q=0.8",
			"app_channel": "ios",
			"user-agent": "manager/5.13.6 (iPhone; iOS 26.0.1; Scale/3.00);deviceName=iPhone;timezone=Asia/Shanghai;model=iPhone18,2;lang=zh-CN;ostype=iOS;clientIdentifier=Domestic",
			"priority": "u=3, i",
			"accept-language": "zh-Hans-HK;q=1.0, en-HK;q=0.9, ja-HK;q=0.8",
		}
		resp = requests.get(url, headers=headers, timeout=10)
		resp.raise_for_status()  # 检查 HTTP 状态码
		vehicle_list_data = resp.json()	# 转换为 Python 字典
		#print(vehicle_list_data)
		if vehicle_list_data.get("status")==0:
			return True
		else:
			print(f"app_token失效:{app_token[:8]}****{app_token[-8:]}")
			return False
	except Exception as err:
		print(f"check_token_valid API调用失败{err}")
		return False

def get_app_token(account_cfg):
	try:
		print("get_app_token(account_cfg)")
		now_ts = int(time.time())
		try:
			token_expire_ts = int(account_cfg.get("token_expires_in") or 0)
		except (ValueError, TypeError):
			token_expire_ts = 0

		try:
			refresh_token_expire_ts = int(account_cfg.get("refresh_token_expires_in") or 0)
		except (ValueError, TypeError):
			refresh_token_expire_ts = 0
		
		app_token = account_cfg.get("access_token") or ""
		refresh_token = account_cfg.get("refresh_token") or ""

		if app_token and now_ts<token_expire_ts:
			if check_token_valid(app_token):
				print(f"app_token:{app_token[:8]}****{app_token[-8:]}")
				return app_token
			#else:
			#	pass
		
		if refresh_token and now_ts<refresh_token_expire_ts:
			app_token_data=refresh_app_token(account_cfg["refresh_token"])
			app_token=app_token_data.get("data",{}).get("access_token")
			if check_token_valid(app_token):
				save_token_to_config(app_token_data)
				print(f"refresh new app_token:{app_token[:8]}****{app_token[-8:]}")
				return app_token
			#else:
			#	pass

		app_token_data=login_get_token(account_cfg["account"],account_cfg["password"])
		app_token=app_token_data.get("data",{}).get("access_token")
		if check_token_valid(app_token):
			save_token_to_config(app_token_data)
			print(f"app_token:{app_token[:8]}****{app_token[-8:]}")
			return app_token
		return None
	except Exception as err:
		print(err)
		return None


class TokenManager:
	def __init__(self, account_cfg_loader):
		"""
		account_cfg_loader: 一个可调用，返回最新的 account_cfg（例如 lambda: get_config("NIU-Account")）
		"""
		self._account_cfg_loader = account_cfg_loader
		self._lock = threading.RLock()
		self._cache_token = ""		  # 内存里的最新 access_token
		self._cache_expire_ts = 0	   # token_expires_in（秒）
		self._last_load_ts = 0		  # 防止过于频繁地去磁盘取
		self._load_interval = 3		 # 每 3s 允许从磁盘重新 load 一次

	def _load_expire_from_cfg(self):
		# 读取磁盘配置里的过期时间，作为近似判断（不用每次都读）
		try:
			cfg = self._account_cfg_loader() or {}
			self._cache_token = cfg.get("access_token") or self._cache_token
			self._cache_expire_ts = int(cfg.get("token_expires_in") or 0)
		except Exception:
			pass

	def invalidate(self):
		"""明确标记内存 token 失效，强制下次 get() 走刷新逻辑"""
		with self._lock:
			self._cache_expire_ts = 0

	def get(self) -> str:
		"""
		返回一个“确保可用”的 token：
		1) 内存里未过期（含提前窗口）则直接用；
		2) 否则调用你已有的 get_app_token()，它会优先 refresh、再 fallback 登录；
		3) 刷新成功后由 save_token_to_config() 落盘，再把内存同步成最新。
		"""
		now = int(time.time())
		with self._lock:
			# 轻量地从磁盘同步一下（避免 token 在别处更新而这里不知道）
			if now - self._last_load_ts >= self._load_interval:
				self._load_expire_from_cfg()
				self._last_load_ts = now

			if self._cache_token and now < (self._cache_expire_ts - EARLY_REFRESH_SECONDS):
				if check_token_valid(self._cache_token):
					return self._cache_token
				else:
					print("缓存Token失效，重新获取")
					pass

			# 走你现有的聚合逻辑（内部已处理：未过期优先、refresh、最后登录）
			account_cfg = self._account_cfg_loader()
			new_token = get_app_token(account_cfg)
			if new_token:
				# 再次同步最新到内存
				try:
					cfg = self._account_cfg_loader() or {}
					self._cache_token = cfg.get("access_token") or new_token
					self._cache_expire_ts = int(cfg.get("token_expires_in") or 0)
				except Exception:
					self._cache_token = new_token
					self._cache_expire_ts = now + 300  # 给个保底 5 分钟
				return self._cache_token

			raise RuntimeError("无法获取可用 token")



