import time
from queue import Queue, Empty
import threading
import logging

from dataclasses import dataclass
import enum, typing

from Shared import MeshCorePrimitives
from Shared.Enums import COM
from Shared.Helpers import runFuncLogged

# helpers
AnyT = typing.TypeVar('AnyT')
def iterQueue(q: Queue[AnyT]) -> typing.Iterator[AnyT]:
		try:
			while 1:
				yield q.get_nowait()
		except Empty:
			return
@ dataclass
class Request:
	command: COM
	payload: dict
	callback: typing.Callable
	blocking: bool
	state: int=0 # 0 waiting, 1 sent, 2 received

class Session:
	def __init__(self):
		self.repeatebleInit()

		self.reqQueue: Queue[Request] = Queue()
		self.requestsToRecv: Queue[Request] = Queue()
		self.responseQueue: Queue[Request] = Queue()
		self.quitNowEvent = threading.Event()

		self.sendThread = threading.Thread(target=lambda: runFuncLogged(self.sendLoop), name='Thread-Send', daemon=True)
		self.sendThread.start()
		self.recvThread = threading.Thread(target=lambda: runFuncLogged(self.recvLoop), name='Thread-Recv', daemon=True)
		self.recvThread.start()
	def repeatebleInit(self):
		self.id: int = 0
		assert len(COM) == 11  # CONNECTION_CHECK removed for P2P
		self.alreadySent: dict[COM, bool] = {COM.CONNECT: False, COM.PAIR: False, COM.OPPONENT_READY: False, COM.GAME_READINESS: False, COM.GAME_WAIT: False, COM.SHOOT: False, COM.OPPONENT_SHOT: False, COM.DISCONNECT: False, COM.AWAIT_REMATCH: False, COM.UPDATE_REMATCH: False}
		self.connected = False # NOTE connected only if active communication w/ opponent is established and will be kept
		self.opponent_node_name: str = None  # Mesh node name of opponent
		self.incoming_messages: Queue[tuple[int, str, dict]] = Queue()  # Queue for incoming meshcore messages

	def setAlreadySent(self, comm: COM):
		assert not self.alreadySent[comm]
		self.alreadySent[comm] = True
	def resetAlreadySent(self, comm: COM):
		self.alreadySent[comm] = False
	def noPendingReqs(self):
		return not any(self.alreadySent.values())
	def fullyDisconnected(self) -> bool:
		return not self.connected and self.noPendingReqs()

	# api --------------------------------------
	def tryToSend(self, command: COM, payload: dict, callback: typing.Callable, *, blocking: bool, mustSend=False) -> bool:
		'''sends the req if it wasn't already sent
		on unsuccesfull send if 'mustSend' it raises RuntimeError, the 'mustSend' best works for requests which post data to the opponent'''
		if sent := not self.alreadySent[command]:
			self._putReq(command, payload, callback, blocking=blocking)
			self.setAlreadySent(command)
		elif mustSend:
			raise RuntimeError("Request specified with 'mustSent' could not be sent due to request being already sent")
		return sent

	def loadResponses(self, *, _drain=False) -> tuple[str, dict]:
		'''gets all available responses and calls callbacks
		the parameter '_drain' should only be used internally
		@return: game end msg supplied from opponent, opponent state on game end'''
		if self.quitNowEvent.is_set() or self.fullyDisconnected(): return '', {}
		gameEndMsg, opponentState = '', None
		for req in iterQueue(self.responseQueue):
			if not req.payload['stay_connected']:
				gameEndMsg = req.payload['game_end_msg']
				self.connected = False
				if 'opponent_grid' in req.payload: opponentState = req.payload['opponent_grid']
			if not _drain: req.callback(req.payload)
			self.resetAlreadySent(req.command)
			self.reqQueue.task_done()
		return gameEndMsg, opponentState
	def _putReq(self, command: COM, payload: dict, callback: typing.Callable, *, blocking: bool):
		assert isinstance(command, enum.Enum) and isinstance(command, str) and isinstance(payload, dict) and callable(callback), 'the request does not meet expected properties'
		# For P2P: CONNECT is now local initialization, PAIR requires opponent_node_name
		assert self.connected or command == COM.CONNECT or (command == COM.PAIR and self.opponent_node_name), 'the session is not connected or no opponent specified'
		assert self.id != 0 or command == COM.CONNECT, 'self.id is invalid for sending this request'
		self.reqQueue.put(Request(command, payload, callback, blocking))
	# checks and closing -----------------------
	def spawnConnectionCheck(self):
		# Connection check not needed for meshcore - messages are fire-and-forget
		# We can detect disconnection by lack of responses
		pass
	def disconnect(self):
		if self.connected and self.opponent_node_name:
			self.tryToSend(COM.DISCONNECT, {}, lambda res: self.repeatebleInit(), blocking=False, mustSend=True)
		self.connected = False
		self.opponent_node_name = None
	def quit(self):
		'''gracefully closes session (recvs last reqs, joins threads), COM.DISCONNECT must have been sent in advance'''
		while not self.noPendingReqs():
			self.loadResponses(_drain=True)
		assert not self.connected, 'the session is still connected'
		self.quitNowEvent.set()
		self.sendThread.join()
		self.recvThread.join()
	def checkThreads(self):
		if not self.sendThread.is_alive():
			raise RuntimeError('Thread-Send ended')
		if not self.recvThread.is_alive():
			raise RuntimeError('Thread-Recv ended')
	# request handling running in threads -------------------------------------
	def sendLoop(self):
		'''waits for reqs from main_thread, sends them and then:
		- for non-blocking: immediately try to fetch response from incoming messages
		- for blocking: move to Thread-Recv for polling'''
		while not self.quitNowEvent.is_set():
			try:
				req = self.reqQueue.get(timeout=1.)
				self._sendReq(req)
				if req.blocking:
					self.requestsToRecv.put(req)
				else:
					# For non-blocking, check if response already arrived
					self._tryFetchNonBlockingResponse(req)
			except Empty:
				pass
	def recvLoop(self):
		'''Poll meshcore for incoming messages and match them to pending requests'''
		pendingReqs: list[Request] = []
		while not self.quitNowEvent.is_set():
			# Get pending blocking requests
			try:
				req = self.requestsToRecv.get(timeout=0.1)
				pendingReqs.append(req)
			except Empty:
				pass
			
			# Poll meshcore for messages
			messages = MeshCorePrimitives.receive_from_meshcore()
			for id_val, command, payload in messages:
				self.incoming_messages.put((id_val, command, payload))
			
			# Process incoming messages
			# First, try to match to pending requests
			self.tryReceiving(pendingReqs)
			
			# Then, process any unmatched messages that might be responses to non-blocking requests
			# or unsolicited messages (like pairing requests)
			self._processUnmatchedMessages()
	
	def tryReceiving(self, pendingReqs: list[Request]):
		'''loops through pendingReqs and tries to match them with incoming messages'''
		doneReqs = []
		# Process all incoming messages
		while not self.incoming_messages.empty():
			id_val, command, payload = self.incoming_messages.get()
			
			# Find matching request
			for req in pendingReqs:
				if req.command == command and req.state == 1:
					req.payload = payload
					self._recvReq(req, id_val, command)
					doneReqs.append(req)
					break
		
		# Remove done requests
		for r in doneReqs:
			pendingReqs.remove(r)
	
	def _tryFetchNonBlockingResponse(self, req: Request):
		'''Try to find and process response for non-blocking request'''
		if req.state != 1:
			return  # Not sent or already received
		
		# Check incoming messages for matching response
		temp_messages = []
		found = False
		while not self.incoming_messages.empty():
			id_val, command, payload = self.incoming_messages.get()
			if command == req.command:
				req.payload = payload
				self._recvReq(req, id_val, command)
				self.responseQueue.put(req)
				found = True
				break
			else:
				temp_messages.append((id_val, command, payload))
		
		# Put back unmatched messages
		for msg in temp_messages:
			self.incoming_messages.put(msg)
		
		# If not found, put request back in recv queue for later polling
		if not found and req.state == 1:
			self.requestsToRecv.put(req)
	
	def _processUnmatchedMessages(self):
		'''Process incoming messages that don't match pending blocking requests'''
		# Check for non-blocking requests waiting in requestsToRecv
		temp_reqs = []
		while not self.requestsToRecv.empty():
			try:
				req = self.requestsToRecv.get_nowait()
				temp_reqs.append(req)
			except Empty:
				break
		
		# Process messages against these requests
		for req in temp_reqs:
			if req.state == 1:  # Already sent
				found = False
				temp_messages = []
				while not self.incoming_messages.empty():
					id_val, command, payload = self.incoming_messages.get()
					if command == req.command:
						req.payload = payload
						self._recvReq(req, id_val, command)
						self.responseQueue.put(req)
						found = True
						break
					else:
						temp_messages.append((id_val, command, payload))
				
				# Put back unmatched messages
				for msg in temp_messages:
					self.incoming_messages.put(msg)
				
				# If not found, put request back
				if not found:
					self.requestsToRecv.put(req)
			else:
				# Not sent yet, put back
				self.requestsToRecv.put(req)
	
	def _fetchResponse(self, req: Request, id_val: int, command: str):
		'''Process received response'''
		self._recvReq(req, id_val, command)
		self.responseQueue.put(req)

	# internals -------------------------------------
	def _sendReq(self, req: Request) -> None:
		assert req.state == 0
		
		# Determine recipient node name
		if req.command == COM.CONNECT:
			# CONNECT is now local initialization, no message sent
			req.state = 1
			return
		
		if not self.opponent_node_name:
			logging.error(f'Cannot send {req.command}: no opponent node name set')
			req.state = 2  # Mark as failed
			return
		
		try:
			# Send via meshcore
			success = MeshCorePrimitives.send_to_node(
				self.opponent_node_name,
				self.id,
				req.command,
				req.payload
			)
			
			if success:
				req.state = 1
			else:
				logging.error(f'Failed to send {req.command} to {self.opponent_node_name}')
				req.state = 2  # Mark as failed
		except Exception as e:
			logging.error(f'Error sending {req.command}: {e}')
			req.state = 2  # Mark as failed
	
	def _recvReq(self, req: Request, id_val: int, command: str):
		assert req.state == 1
		req.state = 2
		
		if command == COM.ERROR:
			logging.error(f'Recvd !ERROR response {req.payload}')
			raise RuntimeError('Recvd !ERROR response')
		
		assert command == req.command, f'Response should have the same command: got {command}, expected {req.command}'
		# For P2P, id validation is less strict - opponent sends their own id
		# We just verify it's not our own id (unless it's a CONNECT response)
		if command != COM.CONNECT and id_val == self.id:
			logging.warning(f'Received message with own id: {id_val}')
