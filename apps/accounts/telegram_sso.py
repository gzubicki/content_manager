import hashlib
import hmac
import time
from django.conf import settings

def verify_telegram_auth(data: dict) -> bool:
    check_hash = data.get('hash')
    auth_data = {k: v for k, v in data.items() if k != 'hash'}
    data_check_string = '\n'.join(f"{k}={auth_data[k]}" for k in sorted(auth_data))
    secret_key = hashlib.sha256(settings.TG_BOT_TOKEN.encode()).digest()
    h = hmac.new(secret_key, msg=data_check_string.encode(), digestmod=hashlib.sha256).hexdigest()
    if h != check_hash:
        return False
    if time.time() - int(auth_data.get('auth_date', '0')) > 86400:
        return False
    return True
