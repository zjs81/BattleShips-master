import subprocess
import json
import logging
import base64
import uuid
from typing import Optional, Tuple, List, Dict

DEBUG_REQS = True
MAX_MESSAGE_SIZE = 100  # Conservative limit for reliability

# In-memory storage for chunk reassembly
_chunk_storage: Dict[str, Dict[int, str]] = {}  # chunk_id -> {chunk_num: data}
_chunk_metadata: Dict[str, Dict] = {}  # chunk_id -> {total_chunks, timestamp}

def _cleanup_old_chunks():
	"""Remove chunks older than 5 minutes"""
	import time
	current_time = time.time()
	to_remove = []
	for chunk_id, metadata in _chunk_metadata.items():
		if current_time - metadata.get('timestamp', 0) > 300:  # 5 minutes
			to_remove.append(chunk_id)
	for chunk_id in to_remove:
		_chunk_storage.pop(chunk_id, None)
		_chunk_metadata.pop(chunk_id, None)

def _chunk_message(message_str: str) -> List[str]:
	"""Split a message into chunks of MAX_MESSAGE_SIZE bytes or smaller"""
	if len(message_str.encode('utf-8')) <= MAX_MESSAGE_SIZE:
		return [message_str]
	
	chunks = []
	message_bytes = message_str.encode('utf-8')
	chunk_id = str(uuid.uuid4())[:8]  # Short ID for chunk metadata
	
	total_chunks = (len(message_bytes) + MAX_MESSAGE_SIZE - 1) // MAX_MESSAGE_SIZE
	
	# Reserve space for chunk metadata in each chunk
	# Format: {"c":chunk_id,"n":num,"t":total,"d":base64_data}
	# Estimate: ~30 bytes for metadata, so ~70 bytes for data
	DATA_CHUNK_SIZE = MAX_MESSAGE_SIZE - 50  # Conservative estimate
	
	for i in range(total_chunks):
		start = i * DATA_CHUNK_SIZE
		end = min(start + DATA_CHUNK_SIZE, len(message_bytes))
		chunk_data = base64.b64encode(message_bytes[start:end]).decode('utf-8')
		
		chunk_msg = {
			'c': chunk_id,
			'n': i,
			't': total_chunks,
			'd': chunk_data
		}
		chunk_str = json.dumps(chunk_msg)
		
		# If still too large, use smaller data chunks
		while len(chunk_str.encode('utf-8')) > MAX_MESSAGE_SIZE:
			DATA_CHUNK_SIZE = max(20, DATA_CHUNK_SIZE - 10)
			start = i * DATA_CHUNK_SIZE
			end = min(start + DATA_CHUNK_SIZE, len(message_bytes))
			chunk_data = base64.b64encode(message_bytes[start:end]).decode('utf-8')
			chunk_msg['d'] = chunk_data
			chunk_str = json.dumps(chunk_msg)
		
		chunks.append(chunk_str)
	
	return chunks

def _reassemble_chunk(chunk_data: Dict) -> Optional[str]:
	"""Reassemble a chunked message. Returns complete message if all chunks received, None otherwise"""
	chunk_id = chunk_data['c']
	chunk_num = chunk_data['n']
	total_chunks = chunk_data['t']
	data = chunk_data['d']
	
	import time
	if chunk_id not in _chunk_storage:
		_chunk_storage[chunk_id] = {}
		_chunk_metadata[chunk_id] = {
			'total_chunks': total_chunks,
			'timestamp': time.time()
		}
	
	_chunk_storage[chunk_id][chunk_num] = data
	_cleanup_old_chunks()
	
	# Check if we have all chunks
	if len(_chunk_storage[chunk_id]) == total_chunks:
		# Reassemble
		chunks = []
		for i in range(total_chunks):
			if i not in _chunk_storage[chunk_id]:
				return None  # Missing chunk
			chunks.append(_chunk_storage[chunk_id][i])
		
		# Decode and combine
		combined_bytes = b''
		for chunk_b64 in chunks:
			combined_bytes += base64.b64decode(chunk_b64)
		
		# Clean up
		_chunk_storage.pop(chunk_id, None)
		_chunk_metadata.pop(chunk_id, None)
		
		return combined_bytes.decode('utf-8')
	
	return None

def send_to_node(node_name: str, player_id: int, command: str, payload: dict = {}) -> bool:
	"""
	Send a message to a meshcore node.
	Returns True if successful, False otherwise.
	"""
	assert isinstance(payload, dict)
	
	msg = {
		'id': player_id,
		'command': command,
		'payload': payload
	}
	msg_str = json.dumps(msg, separators=(',', ':'))  # Compact JSON
	
	if DEBUG_REQS:
		logging.debug(f'sending req to {node_name}: id {player_id}, command {command} payload {payload}')
	
	# Chunk if necessary
	chunks = _chunk_message(msg_str)
	
	for chunk in chunks:
		try:
			# Use meshcli to send message
			# Format: meshcli msg <node_name> "<message>"
			result = subprocess.run(
				['meshcli', 'msg', node_name, chunk],
				capture_output=True,
				text=True,
				timeout=5
			)
			
			if result.returncode != 0:
				logging.error(f'Failed to send message to {node_name}: {result.stderr}')
				return False
		except subprocess.TimeoutExpired:
			logging.error(f'Timeout sending message to {node_name}')
			return False
		except Exception as e:
			logging.error(f'Error sending message to {node_name}: {e}')
			return False
	
	return True

def receive_from_meshcore() -> List[Tuple[int, str, dict]]:
	"""
	Receive messages from meshcore.
	Returns a list of (id, command, payload) tuples.
	"""
	messages = []
	
	try:
		# Use sync_msgs to get all unread messages
		result = subprocess.run(
			['meshcli', '-j', 'sync_msgs'],  # -j for JSON output
			capture_output=True,
			text=True,
			timeout=2
		)
		
		if result.returncode != 0:
			# No messages or error - that's okay
			return messages
		
		# Parse JSON output
		try:
			output_lines = result.stdout.strip().split('\n')
			for line in output_lines:
				if not line.strip():
					continue
				
				try:
					msg_data = json.loads(line)
					# Extract message text
					msg_text = msg_data.get('text', '') or msg_data.get('message', '')
					
					if not msg_text:
						continue
					
					# Check if it's a chunk
					try:
						chunk_data = json.loads(msg_text)
						if 'c' in chunk_data and 'n' in chunk_data and 't' in chunk_data and 'd' in chunk_data:
							# It's a chunk, try to reassemble
							complete_msg = _reassemble_chunk(chunk_data)
							if complete_msg:
								msg_text = complete_msg
							else:
								continue  # Still waiting for more chunks
					except (json.JSONDecodeError, KeyError):
						# Not a chunk, use as-is
						pass
					
					# Parse the actual game message
					try:
						msg = json.loads(msg_text)
						if 'id' in msg and 'command' in msg and 'payload' in msg:
							id_val = msg['id']
							command = msg['command']
							payload = msg['payload']
							
							assert isinstance(id_val, int) and isinstance(command, str) and isinstance(payload, dict)
							
							if DEBUG_REQS:
								logging.debug(f'received req: id {id_val}, command {command} payload {payload}')
							
							messages.append((id_val, command, payload))
					except (json.JSONDecodeError, KeyError, AssertionError) as e:
						logging.debug(f'Failed to parse message: {msg_text}, error: {e}')
						continue
				except json.JSONDecodeError:
					continue
		except Exception as e:
			logging.debug(f'Error parsing meshcore messages: {e}')
	
	except subprocess.TimeoutExpired:
		# Timeout is okay, just return empty list
		pass
	except Exception as e:
		logging.error(f'Error receiving messages from meshcore: {e}')
	
	return messages

def get_contacts() -> List[str]:
	"""
	Get list of available meshcore contacts (node names).
	Returns a list of contact names.
	"""
	try:
		result = subprocess.run(
			['meshcli', '-j', 'contacts'],
			capture_output=True,
			text=True,
			timeout=5
		)
		
		if result.returncode != 0:
			logging.error(f'Failed to get contacts: {result.stderr}')
			return []
		
		# Parse JSON output
		try:
			contacts_data = json.loads(result.stdout)
			# Handle both list and dict formats
			if isinstance(contacts_data, list):
				return [str(c) for c in contacts_data]
			elif isinstance(contacts_data, dict):
				return list(contacts_data.keys())
			else:
				return []
		except json.JSONDecodeError:
			# Try parsing line by line
			contacts = []
			for line in result.stdout.strip().split('\n'):
				if line.strip():
					contacts.append(line.strip())
			return contacts
	except Exception as e:
		logging.error(f'Error getting contacts: {e}')
		return []

def get_own_node_name() -> Optional[str]:
	"""
	Get the name of this node.
	Returns node name or None if unavailable.
	"""
	try:
		result = subprocess.run(
			['meshcli', '-j', 'infos'],
			capture_output=True,
			text=True,
			timeout=5
		)
		
		if result.returncode != 0:
			return None
		
		try:
			info = json.loads(result.stdout)
			# Try common field names for node name
			return info.get('name') or info.get('node_name') or info.get('short_name')
		except (json.JSONDecodeError, KeyError):
			return None
	except Exception:
		return None

def scan_ble_devices(timeout: int = 2) -> List[Dict[str, str]]:
	"""
	Scan for BLE devices.
	Returns a list of dicts with 'name' and 'address' keys.
	"""
	try:
		result = subprocess.run(
			['meshcli', '-j', '-l'],
			capture_output=True,
			text=True,
			timeout=timeout + 1
		)
		
		if result.returncode != 0:
			return []
		
		devices = []
		try:
			# Parse JSON output - could be list or dict
			data = json.loads(result.stdout)
			if isinstance(data, list):
				for item in data:
					if isinstance(item, dict):
						devices.append({
							'name': item.get('name', 'Unknown'),
							'address': item.get('address', '')
						})
					elif isinstance(item, str):
						devices.append({'name': item, 'address': item})
			elif isinstance(data, dict):
				for key, value in data.items():
					devices.append({
						'name': str(value) if isinstance(value, (str, int)) else key,
						'address': key
					})
		except json.JSONDecodeError:
			# Try parsing line by line
			for line in result.stdout.strip().split('\n'):
				if line.strip():
					devices.append({'name': line.strip(), 'address': line.strip()})
		
		return devices
	except Exception as e:
		logging.error(f'Error scanning BLE devices: {e}')
		return []

def test_connection() -> bool:
	"""
	Test if meshcore connection is working.
	Returns True if connection is active, False otherwise.
	"""
	try:
		result = subprocess.run(
			['meshcli', '-j', 'infos'],
			capture_output=True,
			text=True,
			timeout=3
		)
		return result.returncode == 0
	except Exception:
		return False

def connect_ble_device(address: str) -> bool:
	"""
	Connect to a BLE device by address.
	Uses meshcli -S to select device, then stores it in config.
	"""
	try:
		# Use -a to specify address
		result = subprocess.run(
			['meshcli', '-a', address, '-j', 'infos'],
			capture_output=True,
			text=True,
			timeout=5
		)
		return result.returncode == 0
	except Exception as e:
		logging.error(f'Error connecting to BLE device {address}: {e}')
		return False

def connect_tcp(hostname: str, port: int = 5000) -> bool:
	"""
	Connect via TCP/IP.
	"""
	try:
		result = subprocess.run(
			['meshcli', '-t', hostname, '-p', str(port), '-j', 'infos'],
			capture_output=True,
			text=True,
			timeout=5
		)
		return result.returncode == 0
	except Exception as e:
		logging.error(f'Error connecting via TCP {hostname}:{port}: {e}')
		return False

def connect_serial(port: str, baudrate: int = 9600) -> bool:
	"""
	Connect via Serial port.
	"""
	try:
		result = subprocess.run(
			['meshcli', '-s', port, '-b', str(baudrate), '-j', 'infos'],
			capture_output=True,
			text=True,
			timeout=5
		)
		return result.returncode == 0
	except Exception as e:
		logging.error(f'Error connecting via Serial {port}: {e}')
		return False

