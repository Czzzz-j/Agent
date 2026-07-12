import redis
from utils.config_handler import load_redis_config

_redis_client = None

def get_redis_client():
    global _redis_client
    if _redis_client is None:
        cfg = load_redis_config()
        _redis_client = redis.Redis(
            host=cfg.get('host', 'localhost'),
            port=cfg.get('port', 6379),
            db=cfg.get('db', 0),
            socket_timeout=cfg.get('socket_timeout', 5),
            protocol=2
        )
        # 测试连接
        try:
            _redis_client.ping()
        except (redis.ConnectionError, redis.RedisError) as e:
            print(f"Redis 连接失败: {e}，请确保 Redis 服务已启动。")
    return _redis_client
