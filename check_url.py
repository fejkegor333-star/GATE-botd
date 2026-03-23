from src.utils.config import config
print('API URL:', repr(config.gate.api_url))
print('Endpoint:', config.monitoring.contracts_endpoint)
print('Full URL:', repr(config.gate.api_url + config.monitoring.contracts_endpoint))
