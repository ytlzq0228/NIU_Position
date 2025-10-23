import threading
import time

EARLY_REFRESH_SECONDS = 60  # 提前刷新窗口

class TokenManager:
    def __init__(self, account_cfg_loader):
        """
        account_cfg_loader: 一个可调用，返回最新的 account_cfg（例如 lambda: get_config("NIU-Account")）
        """
        self._account_cfg_loader = account_cfg_loader
        self._lock = threading.RLock()
        self._cache_token = ""          # 内存里的最新 access_token
        self._cache_expire_ts = 0       # token_expires_in（秒）
        self._last_load_ts = 0          # 防止过于频繁地去磁盘取
        self._load_interval = 3         # 每 3s 允许从磁盘重新 load 一次

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