from app.core.config import Settings, get_settings

# Test không được đọc .env local (có thể chứa API key thật / provider thật)
Settings.model_config["env_file"] = None
get_settings.cache_clear()
