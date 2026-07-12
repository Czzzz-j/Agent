import mysql.connector.pooling
from utils.config_handler import load_mysql_config

class MySQLClient:
    _pool = None

    @classmethod
    def get_pool(cls):
        if cls._pool is None:
            cfg = load_mysql_config()
            port = cfg.get('port', 3306)
            if isinstance(port, str):
                port = int(port)
            pool_size = cfg.get('pool_size', 5)
            if isinstance(pool_size, str):
                pool_size = int(pool_size)

            cls._pool = mysql.connector.pooling.MySQLConnectionPool(
                pool_name=cfg.get('pool_name', 'agent_pool'),
                pool_size=pool_size,
                host=cfg.get('host', 'localhost'),
                port=port,
                user=cfg.get('user', 'root'),
                password=str(cfg.get('password', '')),
                database=cfg.get('database', 'furniture_agent'),
                use_pure=True   # 避免C扩展类型问题
            )
        return cls._pool

    @classmethod
    def get_connection(cls):
        return cls.get_pool().get_connection()