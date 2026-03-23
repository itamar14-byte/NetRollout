import threading
from dataclasses import dataclass
import ipaddress


@dataclass (slots=True, kw_only=True)
class Device:
	ip: str
	username: str
	password: str
	device_type: str
	secret: str
	port: int

@dataclass (slots=True, kw_only=True)
class RolloutOptions:
	verify: bool = False
	verbose: bool = False
	webapp: bool = False

@dataclass (slots=True, kw_only=True, frozen=True)
class RolloutEngine:
	param: RolloutOptions
	devices: list[Device]
	commands: list[str]
	cancel_event: threading.Event

class WebAppController:
	def __init__(self, ip: str, port: int, run: RolloutEngine) -> None:
		self.ip = ipaddress.ip_address(ip)
		self.port = port
		self.run = run