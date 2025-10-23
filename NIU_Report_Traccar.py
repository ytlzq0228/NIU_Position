import sys
import os
import time
import re
import configparser
import socket
import requests
import json
import threading
from math import sin, cos, sqrt, pi
import requests
from datetime import datetime, timezone
from collections import deque
from config_ops import load_config,get_config,save_token_to_config

FAILED_QUEUE = deque(maxlen=2000)

traccar_config = get_config("Traccar_Config")
TRACCAR_REPORT_INTERVAL=int(traccar_config.get("traccar_report_interval",5))
STILL_SPEED_THRESHOLD=int(traccar_config.get("still_speed_threshold",0))
STILL_REPORT_INTERVALL=int(traccar_config.get("still_report_interval",120))
TRACCAR_URL=traccar_config.get("traccar_url")

EARLY_REFRESH_SECONDS = 60  # 提前刷新窗口

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
				return self._cache_token

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

def save_log(message):
	print(message)

def gcj_to_wgs_exact(lat_gcj, lon_gcj, max_iter=10, tol=1e-7):
	# 椭球与偏心率
	_A = 6378245.0
	_EE = 0.00669342162296594323
	
	def _out_of_china(lat, lon):
		return not (0.8293 < lat < 55.8271 and 72.004 < lon < 137.8347)
	
	def _transform_lat(x, y):
		ret = -100.0 + 2.0*x + 3.0*y + 0.2*y*y + 0.1*x*y + 0.2*sqrt(abs(x))
		ret += (20.0*sin(6.0*x*pi) + 20.0*sin(2.0*x*pi)) * 2.0/3.0
		ret += (20.0*sin(y*pi) + 40.0*sin(y/3.0*pi)) * 2.0/3.0
		ret += (160.0*sin(y/12.0*pi) + 320.0*sin(y*pi/30.0)) * 2.0/3.0
		return ret
	
	def _transform_lon(x, y):
		ret = 300.0 + x + 2.0*y + 0.1*x*x + 0.1*x*y + 0.1*sqrt(abs(x))
		ret += (20.0*sin(6.0*x*pi) + 20.0*sin(2.0*x*pi)) * 2.0/3.0
		ret += (20.0*sin(x*pi) + 40.0*sin(x/3.0*pi)) * 2.0/3.0
		ret += (150.0*sin(x/12.0*pi) + 300.0*sin(x/30.0*pi)) * 2.0/3.0
		return ret
	
	def _delta(lat, lon):
		d_lat = _transform_lat(lon - 105.0, lat - 35.0)
		d_lon = _transform_lon(lon - 105.0, lat - 35.0)
		rad_lat = lat / 180.0 * pi
		magic = 1 - _EE * (sin(rad_lat) ** 2)
		sqrt_magic = sqrt(magic)
		d_lat = (d_lat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * pi)
		d_lon = (d_lon * 180.0) / (_A / sqrt_magic * cos(rad_lat) * pi)
		return d_lat, d_lon
	
	def wgs_to_gcj(lat, lon):
		if _out_of_china(lat, lon):
			return lat, lon
		dlat, dlon = _delta(lat, lon)
		return lat + dlat, lon + dlon
	

	"""迭代反解：使 wgs -> gcj(wgs) ≈ (lat_gcj, lon_gcj)"""
	if _out_of_china(lat_gcj, lon_gcj):
		return lat_gcj, lon_gcj
	lat_wgs, lon_wgs = lat_gcj, lon_gcj  # 初值用 GCJ
	for _ in range(max_iter):
		lat_est, lon_est = wgs_to_gcj(lat_wgs, lon_wgs)
		dlat = lat_gcj - lat_est
		dlon = lon_gcj - lon_est
		lat_wgs += dlat
		lon_wgs += dlon
		if max(abs(dlat), abs(dlon)) < tol:
			break
	return lat_wgs, lon_wgs

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
		#token_data={'data': {'access_token': 'eyJhbGciOiJIUzUxMiIsImtpZCI6IjZTYWxUcmdnUmNHakRXa0hmR3ZSTnZxaTcxWUVHM3M0ZFNRTyIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJpaVFLSzBZU2dkSzdNcFdKWXMxN0lEdUFWanZ3dEpFRHptVnFzS3diMGl1aTFkZWYyNDRMRHd5M3NGZlVhQ1dLIiwiZXhwIjoxNzYxNzQxNjA3LCJpYXQiOjE3NjExMzY4MDc4ODY1NzU3MjIsInN1YiI6IjU3ZWQwYTMxZGY2ZDkwM2IwNTBiOTlkZSJ9.IpGwO3_lZASn2qWMrrFjco4qWGuF45jge2U0XZQ3QTkFuXFDxZVq_iU3MmuaN87yz_ZPZ4y5qAkEvJFrQ6KF-A', 'refresh_token': 'MMUXOTJLZJMTYTDKMI01NDE4LWI1YJYTNZGXNGQ1M2EZZMFJ', 'refresh_token_expires_in': 1763728807, 'token_expires_in': 1761741607}, 'desc': 'ok', 'status': 0}
		print(token_data)
		if token_data.get("status")==0:
			return token_data
		else:
			return None
	except Exception as err:
		save_log(f"API调用失败{err}")
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
		#token_data={'data': {'access_token': 'eyJhbGciOiJIUzUxMiIsImtpZCI6IjZTYWxUcmdnUmNHakRXa0hmR3ZSTnZxaTcxWUVHM3M0ZFNRTyIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJpaVFLSzBZU2dkSzdNcFdKWXMxN0lEdUFWanZ3dEpFRHptVnFzS3diMGl1aTFkZWYyNDRMRHd5M3NGZlVhQ1dLIiwiZXhwIjoxNzYxNzQxNjA3LCJpYXQiOjE3NjExMzY4MDc4ODY1NzU3MjIsInN1YiI6IjU3ZWQwYTMxZGY2ZDkwM2IwNTBiOTlkZSJ9.IpGwO3_lZASn2qWMrrFjco4qWGuF45jge2U0XZQ3QTkFuXFDxZVq_iU3MmuaN87yz_ZPZ4y5qAkEvJFrQ6KF-A', 'refresh_token': 'MMUXOTJLZJMTYTDKMI01NDE4LWI1YJYTNZGXNGQ1M2EZZMFJ', 'refresh_token_expires_in': 1763728807, 'token_expires_in': 1761741607}, 'desc': 'ok', 'status': 0}
		print(token_data)
		if token_data.get("status")==0:
			return token_data
		else:
			return None
	except Exception as err:
		save_log(f"API调用失败{err}")
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
		vehicle_list_data = resp.json()	   # 转换为 Python 字典
		#print(vehicle_list_data)
		if vehicle_list_data.get("status")==0:
			return True
		else:
			return False
	except Exception as err:
		save_log(f"API调用失败{err}")
		return False

def get_app_token(account_cfg):
	try:
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
				return app_token
			else:
				pass
		
		if refresh_token and now_ts<refresh_token_expire_ts:
			app_token_data=refresh_app_token(account_cfg["refresh_token"])
			app_token=app_token_data.get("data",{}).get("access_token")
			if check_token_valid(app_token):
				save_token_to_config(app_token_data)
				return app_token
			else:
				pass

		app_token_data=login_get_token(account_cfg["account"],account_cfg["password"])
		app_token=app_token_data.get("data",{}).get("access_token")
		if check_token_valid(app_token):
			save_token_to_config(app_token_data)
			return app_token
		return None
	except Exception as err:
		return None

def get_vehicle_list(app_token):
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
		vehicle_list_data = resp.json()	   # 转换为 Python 字典
		if vehicle_list_data.get("data",{}).get("items"):
			vehicle_list=[]
			for i in vehicle_list_data.get("data",{}).get("items"):
				vehicle_list.append(i["sn_id"])
		return vehicle_list
	except Exception as err:
		save_log(f"API调用失败{err}")
		return None

def get_vehicle_data(app_token,vehicle_SN):
	url = f"https://app-api.niu.com/v5/scooter/motor_data/index_info?sn={vehicle_SN}"
	
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
		vehicle_data = resp.json()	   # 转换为 Python 字典
		#print(vehicle_data)
		if not vehicle_data.get("data"):
			print("登录失败",vehicle_data)
			return None
		#print(json.dumps(vehicle_data, ensure_ascii=False, indent=2))
		if vehicle_data.get("data",{}).get("isConnected"):
			return vehicle_data
		else:
			return None
	except Exception as err:
		save_log(f"API调用失败{err}")
		return None

def traccar_report(token_mgr,vehicle_SN):

	report_traccar_timestamp=0
	still_report_traccar_timestamp=0
	# 简单的状态码是否重试的判定集合
	RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}
	still_wait_count=61

	while True:
		try:

			# 1) 先处理重试队列：每轮只尝试 1 条，避免长时间阻塞
			now = time.time()
			if FAILED_QUEUE and FAILED_QUEUE[0].get("next_ts", 0) <= now:
				item = FAILED_QUEUE.popleft()
				payload_retry = item.get("payload",{})
				attempts = int(item.get("attempts", 0)) + 1
				try:
					resp = requests.post(TRACCAR_URL, data=payload_retry, timeout=3)  # 重试用短超时
					if 200 <= resp.status_code < 300:
						save_log(f"Traccar Retry OK: id={payload_retry.get('id')} "
								 f"lat={payload_retry.get('lat')} lon={payload_retry.get('lon')} "
								 f"status={resp.status_code}")
					elif resp.status_code in RETRYABLE_HTTP:
						backoff = min(600, 2 ** min(attempts, 10))
						FAILED_QUEUE.append({
							"payload": payload_retry,
							"attempts": attempts,
							"next_ts": now + backoff
						})
						save_log(f"Traccar Retry Defer: http={resp.status_code} "
								 f"attempts={attempts} next={int(backoff)}s "
								 f"queue={len(FAILED_QUEUE)}")
					else:
						save_log(f"Traccar Retry Drop: HTTP {resp.status_code} "
								 f"Body={str(resp.text).strip()[:200]}")
				except Exception as e:
					backoff = min(600, 2 ** min(attempts, 10))
					FAILED_QUEUE.append({
						"payload": payload_retry,
						"attempts": attempts,
						"next_ts": now + backoff
					})
					save_log(f"Traccar Retry Error: {e}; "
							 f"attempts={attempts} next={int(backoff)}s "
							 f"queue={len(FAILED_QUEUE)}")


			# 移动状态逻辑+新点上报
			#if (float(speed) > STILL_SPEED_THRESHOLD and current_timestamp - report_traccar_timestamp >= TRACCAR_REPORT_INTERVAL) or current_timestamp - report_traccar_timestamp >= STILL_LOG_INTERVAL:
			# 2) 到上报周期则发送新点
			current_timestamp=time.time()
			if current_timestamp - report_traccar_timestamp >= TRACCAR_REPORT_INTERVAL:
				report_traccar_timestamp = current_timestamp

				# === 关键：每次调用 API 前现取 token ===
				try:
					app_token = token_mgr.get()
				except Exception as e:
					save_log(f"Get token failed: {e}")
					time.sleep(1)
					continue

				#获取小牛在线数据
				vehicle_data=get_vehicle_data(app_token,vehicle_SN)
				if vehicle_data is None:
					time.sleep(1)
					continue

				lat_gcj = vehicle_data.get("data",{}).get("postion",{}).get("lat")
				lon_gcj = vehicle_data.get("data",{}).get("postion",{}).get("lng")

				if lat_gcj is None or lon_gcj is None:
					time.sleep(1)
					continue
				

				located_time = vehicle_data["data"]["gpsTimestamp"]
				#print(f"GCJ-02 坐标: {lat_gcj}, {lon_gcj}  时间戳: {located_time}")
				lat_wgs, lon_wgs = gcj_to_wgs_exact(lat_gcj, lon_gcj)
				lat = round(lat_wgs, 7)
				lon = round(lon_wgs, 7)

				# 时间戳
				timestamp_ms=int(vehicle_data["data"]["gpsTimestamp"])
				ts = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
				ts_system = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
				#payload={"id": str(vehicle_SN),"timestamp": ts_system}
				#still_wait_count+=1
				
				speed=vehicle_data['data']['nowSpeed']
				
				if float(speed) > STILL_SPEED_THRESHOLD or current_timestamp - still_report_traccar_timestamp > STILL_REPORT_INTERVALL:
					still_report_traccar_timestamp = current_timestamp
				else:
					continue
				
				payload = {
					"id": str(vehicle_SN),
					"lat": f"{float(lat):.7f}",
					"lon": f"{float(lon):.7f}",
					"timestamp": ts
				}
				# km/h -> knots
				payload["speed"] = f"{float(vehicle_data['data']['nowSpeed']) * 3600 / 1852:.2f}"
				#payload["speed"] = f"{float(vehicle_data['data']['nowSpeed']) / 1.852:.2f}"
	
				if vehicle_data.get("data",{}).get("batteries",{}).get("compartmentA",{}).get("batteryCharging") is not None:
					payload["batteryLevel"] = f"{float(vehicle_data['data']['batteries']['compartmentA']['batteryCharging']):.1f}"
				else:
					payload["batteryLevel"] = 0
	
				if vehicle_data.get("data",{}).get("gps") is not None:
					payload["sat"] = vehicle_data['data']['gps']

				if vehicle_data.get("data",{}).get("gsm") is not None:
					payload["rssi"] = vehicle_data['data']['gsm']
	
				if vehicle_data.get("data",{}).get("hdop") is not None:
					hdop = float(vehicle_data['data']["hdop"])
					payload["accuracy"] = f"{max(0.0, hdop * 5.0):.1f}"  # 经验值，可按需要调整或移除
					payload["hdop"] = f"{hdop:.2f}"					  # 同时把原始 hdop 也带上
	

				if vehicle_data.get("data",{}).get("isAccOn") is not None:
					payload["ignition"] = 1 if int(vehicle_data['data']["isAccOn"]) == 1 else 0
				
				if vehicle_data.get("data",{}).get("isCharging") is not None:
					payload["charge"] = 1 if int(vehicle_data['data']["isCharging"]) == 1 else 0

				#print(payload)
				try:
					resp = requests.post(TRACCAR_URL, data=payload, timeout=3)
					if 200 <= resp.status_code < 300:
						print(f"Traccar Report OK: id={vehicle_SN} payload: {payload}")
					elif resp.status_code in RETRYABLE_HTTP:
						if "timestamp" in payload:
							FAILED_QUEUE.append({"payload": payload, "attempts": 0, "next_ts": time.time() + 1})
						save_log(f"Traccar Report Enqueue (HTTP {resp.status_code}) queue={len(FAILED_QUEUE)}")
					else:
						save_log(f"Traccar Report Fail: HTTP {resp.status_code} "
								 f"Body={str(resp.text).strip()[:200]}")
				except Exception as req_err:
					if "timestamp" in payload:
						FAILED_QUEUE.append({"payload": payload, "attempts": 0, "next_ts": time.time() + 1})
					save_log(f"Traccar Report Request Error: {req_err}; queued={len(FAILED_QUEUE)}")

			time.sleep(0.1)

		except Exception as loop_err:
			raise
			save_log(f"Traccar Report Error: {loop_err}")
			time.sleep(1)

def start_traccar_thread(token_mgr, sn):
	t = threading.Thread(
		target=traccar_report,
		args=(token_mgr, sn),
		name=f"traccar_{sn}",
		daemon=True  # 主进程退出时自动结束
	)
	t.start()
	return t

def NIU_report_traccar():
	# 用一个 loader，确保每次都能拿到最新配置（save_token_to_config 落盘后可读到）
	token_mgr = TokenManager(lambda: get_config("NIU-Account"))

	# 取一次当前可用 token 获取车辆列表
	app_token = token_mgr.get()
	vehicle_list=get_vehicle_list(app_token)
	
	for sn in vehicle_list:
		start_traccar_thread(token_mgr, sn)
	try:
		while True:
			time.sleep(3600)
	except KeyboardInterrupt:
		pass


if __name__ == "__main__":
	NIU_report_traccar()
	#refresh_app_token("MDLMZDQ5MMITOGRMMY01NWMZLTGXMTUTMGQ2ZWE0YTDJMDRK")
	#get_token(1,2)

