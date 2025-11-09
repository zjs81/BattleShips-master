import logging, sys, string
from pygame import Rect, mouse
import pygame
from typing import TypeVar, Optional
import random

from . import Constants
from . import Frontend
from .Session import Session
from Shared.Enums import SHOTS, STAGES, COM
from Shared import MeshCorePrimitives

class Game:
	def __init__(self):
		self.session = Session()
		self.options = Options()
		self.redrawNeeded = True
		self.gameStage: STAGES = STAGES.MAIN_MENU
		self.repeatableInit()
		if '--autoplay' in sys.argv: self.newGameStage(STAGES.CONNECTING)
	def repeatableInit(self, keepConnection=False):
		self.grid = Grid(True)
		self.opponentGrid = Grid(False)
		self.options.repeatableInit()
		self.transition: Transition = None
		if not keepConnection: self.session.repeatebleInit()
		
		# P2P game state
		self.opponent_game_state: dict = {'ready': False, 'ships': []}  # Opponent's game state
		self.player_on_turn: int = 0  # 0 = not started, otherwise player ID
		self.last_shotted_pos = [-1, -1]  # Last position opponent shot at
		self.game_active: bool = True
	def quit(self):
		logging.info('Closing due to client quit')
		if self.session.connected: self.session.disconnect()
		self.newGameStage(STAGES.CLOSING)
	def newGameStage(self, stage: STAGES):
		assert STAGES.COUNT == 12  # Added RADIO_CONNECTION
		assert stage != self.gameStage
		self.gameStage = stage
		logging.debug(f'New game stage: {str(stage)}')
		Frontend.Runtime.resetVars()
		self.options.hudMsg = ''
		self.redrawNeeded = True
		if self.gameStage == STAGES.CONNECTING:
			self.repeatableInit()
		elif self.gameStage == STAGES.GAME_END and '--autoplay' in sys.argv:
			pygame.time.set_timer(pygame.QUIT, 1000, 1)
		elif self.gameStage == STAGES.PLACING:
			self.redrawHUD()
			if '--autoplace' in sys.argv:
				self.grid.autoplace()
				if self.options.firstGameWait: self.toggleGameReady()
		elif self.gameStage in [STAGES.GAME_WAIT, STAGES.SHOOTING]:
			self.redrawHUD()
		elif self.gameStage == STAGES.MAIN_MENU:
			if self.session.connected: self.session.disconnect()
			# Clear static menu cache when entering main menu to force fresh render
			Frontend.Runtime._staticMenuCache = None
			Frontend.Runtime._headerNeedsRedraw = True
		elif self.gameStage == STAGES.RADIO_CONNECTION:
			# Initialize radio connection state
			if not hasattr(self.options, 'radioConnectionType'):
				self.options.radioConnectionType = 'BLE'  # Default to BLE
			if not hasattr(self.options, 'bleDevices'):
				self.options.bleDevices = []
			if not hasattr(self.options, 'selectedDeviceIndex'):
				self.options.selectedDeviceIndex = -1
			if not hasattr(self.options, 'tcpHostname'):
				self.options.tcpHostname = []
			if not hasattr(self.options, 'tcpPort'):
				self.options.tcpPort = ['5', '0', '0', '0']  # Default 5000
			if not hasattr(self.options, 'serialPort'):
				self.options.serialPort = []
			if not hasattr(self.options, 'connectionStatus'):
				self.options.connectionStatus = 'Not connected'
			# Scan for BLE devices
			if self.options.radioConnectionType == 'BLE' and not self.options.bleDevices:
				self._scanBLEDevices()
	def changeGridShown(self, my:bool=None, *, transition=False):
		if my is None: my = not self.options.myGridShown
		if transition:
			assert self.gameStage == STAGES.SHOOTING
			self.transition = Transition(my)
		else: self.options.myGridShown = my
		self.redrawHUD()

	# requests -------------------------------------------------
	def connectCallback(self, res):
		# For P2P: CONNECT is now local initialization
		# Generate own ID (simple hash of name + random)
		import hashlib
		name = self.options.submittedPlayerName()
		name_hash = int(hashlib.md5(name.encode()).hexdigest()[:8], 16)
		self.session.id = (name_hash % (2**20 - 1000)) + 1000  # Same ID range as before
		self.session.connected = True
		logging.info(f'Initialized as player {self.session.id} ({name})')
		self.newGameStage(STAGES.PAIRING)
	def pairCallback(self, res, rematched=False):
		if ('paired' in res and res['paired']) or (rematched and 'rematched' in res and res['rematched']):
			verb = 'Rematched' if rematched else 'Paired'
			logging.info(f"{verb} with {res['opponent']['id']} - '{res['opponent']['name']}'")
			self.options.opponentName = res['opponent']['name']
			self.newGameStage(STAGES.PLACING)
			self.options.hudMsg = f"{verb} with {res['opponent']['name']}"
	def opponentReadyCallback(self, res):
		self.options.opponentReady = res['opponent_ready']
		self.redrawHUD()
	def gameReadiness(self):
		assert self.gameStage in [STAGES.PLACING, STAGES.GAME_WAIT]
		if self.session.alreadySent[COM.GAME_READINESS]: return
		wasPlacing = self.gameStage == STAGES.PLACING
		if wasPlacing: self.newGameStage(STAGES.GAME_WAIT)
		our_state = {'ships': self.grid.shipsDicts(), 'ready': wasPlacing, 'id': self.session.id}
		lamda = lambda res: self.gameReadinessCallback(wasPlacing, res, our_state)
		self.session.tryToSend(COM.GAME_READINESS, our_state, lamda, blocking=False, mustSend=True)
	def gameReadinessCallback(self, wasPlacing, res, our_state):
		# For P2P: Update opponent state from response
		if 'opponent_state' in res:
			self.opponent_game_state = res['opponent_state']
			self.options.opponentReady = self.opponent_game_state.get('ready', False)
		
		# Check if both ready to start shooting
		our_ready = our_state.get('ready', False)
		opponent_ready = self.opponent_game_state.get('ready', False)
		if wasPlacing and opponent_ready and our_ready:
			self._startShooting()
		elif not wasPlacing and res.get('approved', False):
			self.newGameStage(STAGES.PLACING)
		else:
			self.redrawHUD()
	
	def _handleRadioConnectionClick(self, mousePos):
		'''Handle mouse clicks in radio connection menu'''
		# Connection type buttons - match new layout
		conn_types = ['BLE', 'TCP', 'Serial']
		section_y = 80 + 80  # title_y + 80
		conn_y = section_y + 50
		conn_button_width = 140
		conn_button_height = 45
		conn_spacing = 20
		total_width = len(conn_types) * conn_button_width + (len(conn_types) - 1) * conn_spacing
		conn_start_x = (Constants.SCREEN_WIDTH - total_width) // 2
		
		for i, conn_type in enumerate(conn_types):
			rect = Rect(conn_start_x + i * (conn_button_width + conn_spacing), conn_y, conn_button_width, conn_button_height)
			if rect.collidepoint(mousePos):
				self.options.radioConnectionType = conn_type
				self.options.selectedDeviceIndex = -1
				# Reset input fields
				if conn_type == 'TCP':
					if not hasattr(self.options, 'tcpHostname'):
						self.options.tcpHostname = []
					if not hasattr(self.options, 'tcpPort'):
						self.options.tcpPort = ['5', '0', '0', '0']
					if not hasattr(self.options, 'tcpHostnameActive'):
						self.options.tcpHostnameActive = False
						self.options.tcpHostnameCursor = 0
					if not hasattr(self.options, 'tcpPortActive'):
						self.options.tcpPortActive = False
						self.options.tcpPortCursor = 0
				elif conn_type == 'Serial':
					if not hasattr(self.options, 'serialPort'):
						self.options.serialPort = []
					if not hasattr(self.options, 'serialPortActive'):
						self.options.serialPortActive = False
						self.options.serialPortCursor = 0
				elif conn_type == 'BLE':
					# Scan for devices
					self._scanBLEDevices()
				self.redrawNeeded = True
				return
		
		# BLE device selection - match new layout
		if self.options.radioConnectionType == 'BLE' and self.options.bleDevices:
			content_y = conn_y + conn_button_height + 40
			list_y = content_y + 40
			list_rect = Rect(50, list_y, Constants.SCREEN_WIDTH - 100, 200)
			
			if list_rect.collidepoint(mousePos):
				device_item_height = 35
				max_visible = min(5, len(self.options.bleDevices))
				start_idx = max(0, min(self.options.selectedDeviceIndex - 2, len(self.options.bleDevices) - max_visible))
				end_idx = min(len(self.options.bleDevices), start_idx + max_visible)
				
				for i in range(start_idx, end_idx):
					device_y = list_rect.y + 10 + (i - start_idx) * device_item_height
					device_rect = Rect(list_rect.x + 10, device_y, list_rect.width - 20, device_item_height - 5)
					if device_rect.collidepoint(mousePos):
						self.options.selectedDeviceIndex = i
						self.redrawNeeded = True
						return
		
		# TCP hostname input - match new layout
		if self.options.radioConnectionType == 'TCP':
			content_y = conn_y + conn_button_height + 40
			label_y = content_y
			hostname_box = Rect(Constants.SCREEN_WIDTH // 2 - 200, label_y + 40, 400, 45)
			if hostname_box.collidepoint(mousePos):
				self.options.tcpHostnameActive = True
				self.options.tcpPortActive = False
				if not hasattr(self.options, 'tcpHostnameCursor'):
					self.options.tcpHostnameCursor = len(self.options.tcpHostname)
				self.redrawNeeded = True
				return
			
			port_label_y = label_y + 100
			port_box = Rect(Constants.SCREEN_WIDTH // 2 - 100, port_label_y + 40, 200, 45)
			if port_box.collidepoint(mousePos):
				self.options.tcpPortActive = True
				self.options.tcpHostnameActive = False
				if not hasattr(self.options, 'tcpPortCursor'):
					self.options.tcpPortCursor = len(self.options.tcpPort)
				self.redrawNeeded = True
				return
		
		# Serial port input - match new layout
		if self.options.radioConnectionType == 'Serial':
			content_y = conn_y + conn_button_height + 40
			label_y = content_y
			port_box = Rect(Constants.SCREEN_WIDTH // 2 - 200, label_y + 40, 400, 45)
			if port_box.collidepoint(mousePos):
				self.options.serialPortActive = True
				if not hasattr(self.options, 'serialPortCursor'):
					self.options.serialPortCursor = len(self.options.serialPort)
				self.redrawNeeded = True
				return
		
		# Action buttons - match new layout
		button_y = Constants.SCREEN_HEIGHT - 80
		button_width = 160
		button_height = 45
		button_spacing = 20
		
		# Connect button
		connect_rect = Rect((Constants.SCREEN_WIDTH - button_width * 2 - button_spacing) // 2, button_y, button_width, button_height)
		if connect_rect.collidepoint(mousePos):
			self._attemptRadioConnection()
			return
		
		# Refresh button (BLE)
		if self.options.radioConnectionType == 'BLE':
			refresh_rect = Rect(connect_rect.right + button_spacing, button_y, button_width, button_height)
			if refresh_rect.collidepoint(mousePos):
				self._scanBLEDevices()
				return
		
		# Back button
		back_y = button_y + button_height + 15
		back_rect = Rect((Constants.SCREEN_WIDTH - button_width) // 2, back_y, button_width, button_height)
		if back_rect.collidepoint(mousePos):
			self.newGameStage(STAGES.MULTIPLAYER_MENU)
			return
		
		# Click outside input fields deactivates them
		if hasattr(self.options, 'tcpHostnameActive'):
			self.options.tcpHostnameActive = False
		if hasattr(self.options, 'tcpPortActive'):
			self.options.tcpPortActive = False
		if hasattr(self.options, 'serialPortActive'):
			self.options.serialPortActive = False
	
	def _attemptRadioConnection(self):
		'''Attempt to connect to radio based on selected options'''
		self.options.connectionStatus = 'Connecting...'
		self.redrawNeeded = True
		
		success = False
		if self.options.radioConnectionType == 'BLE':
			if self.options.selectedDeviceIndex >= 0 and self.options.selectedDeviceIndex < len(self.options.bleDevices):
				device = self.options.bleDevices[self.options.selectedDeviceIndex]
				address = device.get('address', '')
				if address:
					success = MeshCorePrimitives.connect_ble_device(address)
		elif self.options.radioConnectionType == 'TCP':
			hostname = ''.join(self.options.tcpHostname) or 'localhost'
			port_str = ''.join(self.options.tcpPort) or '5000'
			try:
				port = int(port_str)
				success = MeshCorePrimitives.connect_tcp(hostname, port)
			except ValueError:
				self.options.connectionStatus = 'Invalid port number'
				return
		elif self.options.radioConnectionType == 'Serial':
			port = ''.join(self.options.serialPort) or '/dev/ttyUSB0'
			success = MeshCorePrimitives.connect_serial(port)
		
		if success:
			self.options.connectionStatus = 'Connected!'
			# Test connection
			if MeshCorePrimitives.test_connection():
				logging.info('Radio connection successful')
				self.newGameStage(STAGES.CONNECTING)
			else:
				self.options.connectionStatus = 'Connection failed - device not responding'
		else:
			self.options.connectionStatus = 'Connection failed - check device and try again'
	
	def _initiatePairing(self):
		'''Initiate pairing by getting contacts and sending pairing request'''
		contacts = MeshCorePrimitives.get_contacts()
		if not contacts:
			logging.warning('No contacts available for pairing')
			return
		
		# For now, pair with first available contact
		# TODO: Add UI for contact selection
		if len(contacts) > 0:
			opponent_name = contacts[0]
			self.session.opponent_node_name = opponent_name
			logging.info(f'Attempting to pair with {opponent_name}')
			self.session.tryToSend(COM.PAIR, {'name': self.options.submittedPlayerName(), 'id': self.session.id}, self.pairCallback, blocking=True)
	
	def _startShooting(self):
		'''Start shooting phase - determine who goes first'''
		logging.info('Starting shooting phase')
		self.gameStage = STAGES.SHOOTING
		# Randomly determine who goes first
		players = [self.session.id, self.opponent_game_state.get('id', 0)]
		self.player_on_turn = random.choice(players)
		self.grid.initShipSizes()
		self.changeGridShown(self.player_on_turn != self.session.id, transition=self.player_on_turn == self.session.id)
	def gameWaitCallback(self, res):
		if res.get('started', False):
			self.grid.initShipSizes()
			self.newGameStage(STAGES.SHOOTING)
			self.player_on_turn = res.get('on_turn', self.session.id)
			self.changeGridShown(res['on_turn'] != self.session.id, transition=res['on_turn'] == self.session.id)
			logging.info('Shooting started')
	def shootReq(self, gridPos):
		assert self.gameStage == STAGES.SHOOTING
		# Validate it's our turn
		if self.player_on_turn != self.session.id:
			logging.warning('Not your turn!')
			return
		
		# Validate locally first
		hitted, sunkenShip, gameWon = self._validateShoot(gridPos)
		
		# Send shot to opponent
		callback = lambda res: self.shootCallback(gridPos, res, hitted, sunkenShip, gameWon)
		self.session.tryToSend(COM.SHOOT, {'pos': gridPos}, callback, blocking=False, mustSend=True)
	
	def _validateShoot(self, pos) -> tuple[bool, Optional['Ship'], bool]:
		'''Validate shot against opponent's grid state. Returns (hitted, sunkenShip, gameWon)'''
		# Check opponent's ships
		for ship_dict in self.opponent_game_state.get('ships', []):
			x, y = ship_dict['pos']
			horizontal = ship_dict['horizontal']
			size = ship_dict['size']
			
			if (x <= pos[0] <= x + (size - 1) * horizontal) and (y <= pos[1] <= y + (size - 1) * (not horizontal)):
				hittedSpot = (pos[0] - x) if horizontal else (pos[1] - y)
				# Mark as hit
				if 'hitted' not in ship_dict:
					ship_dict['hitted'] = [False] * size
				ship_dict['hitted'][hittedSpot] = True
				
				sunkenShip = ship_dict if all(ship_dict['hitted']) else None
				gameWon = all([all(s.get('hitted', [False]) for s in self.opponent_game_state.get('ships', []))])
				return True, Ship.fromDict(sunkenShip) if sunkenShip else None, gameWon
		
		return False, None, False
	
	def shootCallback(self, gridPos, res, hitted, sunkenShip, gameWon):
		# Update opponent grid with shot result
		self.opponentGrid.gotShotted(gridPos, hitted, sunkenShip)
		
		# Switch turns
		self.player_on_turn = self.opponent_game_state.get('id', 0)
		
		self.changeGridShown(transition=not gameWon)
		if gameWon:
			self.newGameStage(STAGES.GAME_END)
			logging.info('Game won')
			self.options.gameEndMsg = 'You won!   :)'
			if 'opponent_grid' in res:
				self.opponentGrid.updateAfterGameEnd(res['opponent_grid'])
	def gettingShotCallback(self, res):
		if not res.get('shotted', False): return
		
		pos = res['pos']
		self.last_shotted_pos = pos
		
		# Process shot on our grid
		hitted, sunkenShip = self.grid.localGridShotted(pos, update=True)
		self.grid.gotShotted(pos, hitted, sunkenShip)
		
		# Check if we lost
		lost = all([all(ship.hitted) for ship in self.grid.ships])
		
		# Send response to opponent
		response_payload = {
			'hitted': hitted,
			'sunken_ship': sunkenShip.asDict() if sunkenShip else None,
			'game_won': lost
		}
		if lost:
			response_payload['opponent_grid'] = {'ships': self.grid.shipsDicts()}
			response_payload['game_end_msg'] = 'You lost!   :('
		
		# Send response (this is handled by the session when it receives SHOOT command)
		# For now, we'll handle it in the message processing
		
		self.changeGridShown(transition=not lost)
		if lost:
			logging.info('Game lost')
			self.newGameStage(STAGES.GAME_END)
			self.options.gameWon = False
			self.options.gameEndMsg = 'You lost!   :('
			# Switch turn back (game over)
			self.player_on_turn = 0

	def sendUpdateRematch(self, rematchDesired):
		if self.session.alreadySent[COM.UPDATE_REMATCH]: return
		self.options.awaitingRematch = True
		lamda = lambda res: self.rematchCallback(rematchDesired, res)
		self.session.tryToSend(COM.UPDATE_REMATCH, {'rematch_desired': rematchDesired}, lamda, blocking=False, mustSend=True)
	def rematchCallback(self, rematchDesired, res):
		if res['approved']: self.options.awaitingRematch = rematchDesired
		if 'rematched' in res and res['rematched']:
			self.execRematch(res)
		self.redrawNeeded = True
	def awaitRematchCallback(self, res):
		if not res['changed']: return
		self.redrawNeeded = True
		if 'opponent_disconnected' in res and res['opponent_disconnected']:
			assert res['stay_connected']
			self.options.rematchPossible = False
			self.session.disconnect()
		elif 'rematched' in res and res['rematched']:
			self.execRematch(res)
		elif 'opponent_rematching' in res:
			self.options.opponentRematching = res['opponent_rematching']
		else: assert False, 'Changed field expected'
	def execRematch(self, res: dict):
		assert 'opponent' in res
		self.repeatableInit(True)
		self.pairCallback(res, True)

	def handleConnections(self):
		self.session.checkThreads()
		self.handleResponses()
		self.spawnReqs()
	def handleResponses(self):
		assert len(COM) == 11  # CONNECTION_CHECK removed for P2P
		gameEndMsg, opponentState = self.session.loadResponses()
		if self.gameStage in [STAGES.MAIN_MENU, STAGES.GAME_END]:
			if '--autoplay-repeat' in sys.argv and self.session.fullyDisconnected():
				logging.info('Autoplay repeat')
				self.newGameStage(STAGES.CONNECTING)
			return
		elif self.gameStage == STAGES.CLOSING:
			self.session.quit()
		elif gameEndMsg and self.gameStage not in [STAGES.GAME_END, STAGES.END_GRID_SHOW]: # NOTE unstandard game end
			logging.warning(f"Opponent disconnected: '{gameEndMsg}'")
			self.options.gameEndMsg = gameEndMsg
			if opponentState is not None and 'ships' in opponentState: self.opponentGrid.updateAfterGameEnd(opponentState)
			self.newGameStage(STAGES.GAME_END)
	def _scanBLEDevices(self):
		'''Scan for BLE devices'''
		self.options.connectionStatus = 'Scanning for devices...'
		self.redrawNeeded = True
		# Scan will happen in next frame to avoid blocking
	
	def spawnReqs(self):
		assert STAGES.COUNT == 12  # Added RADIO_CONNECTION
		if self.gameStage == STAGES.RADIO_CONNECTION:
			# Handle radio connection GUI interactions
			if self.options.connectionStatus == 'Scanning for devices...':
				self.options.bleDevices = MeshCorePrimitives.scan_ble_devices(timeout=2)
				if self.options.bleDevices:
					self.options.connectionStatus = f'Found {len(self.options.bleDevices)} device(s)'
				else:
					self.options.connectionStatus = 'No devices found. Click Refresh to scan again.'
				self.redrawNeeded = True
		elif self.gameStage == STAGES.CONNECTING:
			# CONNECT is now local - just initialize
			if not self.session.connected:
				self.connectCallback({})  # Empty response since it's local
		elif self.gameStage == STAGES.PAIRING:
			# For P2P: Get contacts and allow pairing
			if not hasattr(self, '_pairing_initiated'):
				self._pairing_initiated = True
				self._initiatePairing()
		elif self.gameStage == STAGES.PLACING:
			self.session.tryToSend(COM.OPPONENT_READY, {'expected': self.options.opponentReady}, self.opponentReadyCallback, blocking=True)
		elif self.gameStage == STAGES.GAME_WAIT:
			self.session.tryToSend(COM.GAME_WAIT, {}, self.gameWaitCallback, blocking=True)
		elif self.gameStage == STAGES.SHOOTING and self.options.myGridShown:
			self.session.tryToSend(COM.OPPONENT_SHOT, {}, self.gettingShotCallback, blocking=True)
		elif self.gameStage in [STAGES.GAME_END, STAGES.END_GRID_SHOW] and self.session.connected and self.options.rematchPossible:
			self.session.tryToSend(COM.AWAIT_REMATCH, {'expected_opponent_rematch': self.options.opponentRematching}, self.awaitRematchCallback, blocking=True)
		self.session.spawnConnectionCheck()

	# controls and API -------------------------------------------------
	def rotateShip(self):
		if self.gameStage == STAGES.PLACING:
			self.grid.rotateShip()
			self.redrawNeeded = True
	def changeCursor(self):
		if self.gameStage == STAGES.PLACING and not self.grid.allShipsPlaced():
			self.grid.changeCursor(mouse.get_pos())
			self.redrawNeeded = True
	def mouseClick(self, mousePos, rightClick=False):
		if rightClick and self.gameStage != STAGES.PLACING: return
		if mousePos[1] <= Constants.HUD_RECT.bottom: self.grid.removeShipInCursor()
		self.redrawNeeded = True
		self.options.hudMsg = ''
		if Constants.HEADER_CLOSE_RECT.collidepoint(mousePos):
			self.quit()
		elif Constants.HEADER_MINIMIZE_RECT.collidepoint(mousePos):
			pygame.display.iconify()
		elif Frontend.grabWindow(mousePos):
			self.options.inputActive = False
		elif self.gameStage == STAGES.RADIO_CONNECTION:
			self._handleRadioConnectionClick(mousePos)
		elif self.gameStage == STAGES.MULTIPLAYER_MENU: self.options.mouseClick(mousePos)
		elif not rightClick and Frontend.HUDReadyCollide(mousePos, True):
			if self.gameStage == STAGES.END_GRID_SHOW: self.newGameStage(STAGES.GAME_END)
			else: self.toggleGameReady()
		elif not rightClick and (size := Frontend.HUDShipboxCollide(mousePos, True)):
			self.grid.changeSize(+1, canBeSame=True, currSize=size)
		elif self.gameStage == STAGES.GAME_END and (res := Frontend.thumbnailCollide(mousePos, True))[0]:
			self.newGameStage(STAGES.END_GRID_SHOW)
			self.changeGridShown(my=res[1] == 0)
		elif self.gameStage == STAGES.GAME_END and Constants.REMATCH_BTN_RECT.collidepoint(mousePos):
			self.toggleRematch()
		elif self.gameStage == STAGES.PLACING:
			changed = self.grid.mouseClick(mousePos, rightClick)
			if changed:
				self.redrawHUD()
				if self.options.firstGameWait: self.toggleGameReady()
		elif self.gameStage == STAGES.SHOOTING:
			self.shoot(mousePos)
	def mouseMovement(self, event):
		if Frontend.Runtime.windowGrabbedPos:
			Frontend.moveWindow(event.pos)
			# Skip other processing during window drag to reduce lag
			return
		elif Frontend.HUDReadyCollide(event.pos) or Frontend.HUDShipboxCollide(event.pos): self.redrawHUD()
		elif Frontend.headerBtnCollide(event.pos): self.redrawNeeded = True
		elif self.gameStage == STAGES.GAME_END and Frontend.thumbnailCollide(event.pos): self.redrawNeeded = True
		else: self.redrawNeeded |= self.grid.flyingShip.size
	def keydownInMenu(self, event):
		self.redrawNeeded = True
		if event.key in [pygame.K_RETURN, pygame.K_KP_ENTER]:
			if self.gameStage == STAGES.RADIO_CONNECTION:
				# Try to connect
				self._attemptRadioConnection()
			else:
				stageChanges = {STAGES.MAIN_MENU: STAGES.MULTIPLAYER_MENU, STAGES.MULTIPLAYER_MENU: STAGES.RADIO_CONNECTION, STAGES.GAME_END: STAGES.MAIN_MENU, STAGES.END_GRID_SHOW: STAGES.GAME_END}
				if self.options.inputActive: self.options.inputActive = False
				else: self.newGameStage(stageChanges[self.gameStage])
		elif event.key in [pygame.K_LEFT, pygame.K_RIGHT]:
			if self.gameStage == STAGES.RADIO_CONNECTION:
				# Handle radio connection navigation
				if self.options.radioConnectionType == 'BLE' and self.options.bleDevices:
					if event.key == pygame.K_UP or (event.key == pygame.K_LEFT and self.options.selectedDeviceIndex > 0):
						self.options.selectedDeviceIndex = max(0, self.options.selectedDeviceIndex - 1)
					elif event.key == pygame.K_DOWN or (event.key == pygame.K_RIGHT and self.options.selectedDeviceIndex < len(self.options.bleDevices) - 1):
						self.options.selectedDeviceIndex = min(len(self.options.bleDevices) - 1, self.options.selectedDeviceIndex + 1)
			else:
				self.options.moveCursor([-1, 1][event.key == pygame.K_RIGHT])
		elif event.key in [pygame.K_BACKSPACE, pygame.K_DELETE]:
			if self.gameStage == STAGES.RADIO_CONNECTION:
				# Handle input field deletion
				if hasattr(self.options, 'tcpHostnameActive') and self.options.tcpHostnameActive:
					if len(self.options.tcpHostname) > 0:
						if not event.key == pygame.K_DELETE:
							self.options.tcpHostnameCursor = max(0, self.options.tcpHostnameCursor - 1)
							self.options.tcpHostname.pop(self.options.tcpHostnameCursor)
						else:
							if self.options.tcpHostnameCursor < len(self.options.tcpHostname):
								self.options.tcpHostname.pop(self.options.tcpHostnameCursor)
				elif hasattr(self.options, 'tcpPortActive') and self.options.tcpPortActive:
					if len(self.options.tcpPort) > 0:
						if not event.key == pygame.K_DELETE:
							self.options.tcpPortCursor = max(0, self.options.tcpPortCursor - 1)
							self.options.tcpPort.pop(self.options.tcpPortCursor)
						else:
							if self.options.tcpPortCursor < len(self.options.tcpPort):
								self.options.tcpPort.pop(self.options.tcpPortCursor)
				elif hasattr(self.options, 'serialPortActive') and self.options.serialPortActive:
					if len(self.options.serialPort) > 0:
						if not event.key == pygame.K_DELETE:
							self.options.serialPortCursor = max(0, self.options.serialPortCursor - 1)
							self.options.serialPort.pop(self.options.serialPortCursor)
						else:
							if self.options.serialPortCursor < len(self.options.serialPort):
								self.options.serialPort.pop(self.options.serialPortCursor)
			else:
				self.options.removeChar(event.key == pygame.K_DELETE)
		elif event.key in [pygame.K_UP, pygame.K_DOWN]:
			if self.gameStage == STAGES.RADIO_CONNECTION and self.options.radioConnectionType == 'BLE':
				if self.options.bleDevices:
					if event.key == pygame.K_UP:
						self.options.selectedDeviceIndex = max(0, self.options.selectedDeviceIndex - 1)
					elif event.key == pygame.K_DOWN:
						self.options.selectedDeviceIndex = min(len(self.options.bleDevices) - 1, self.options.selectedDeviceIndex + 1)
		else:
			if self.gameStage == STAGES.RADIO_CONNECTION:
				# Handle input field typing
				if hasattr(self.options, 'tcpHostnameActive') and self.options.tcpHostnameActive:
					if len(self.options.tcpHostname) < 50:  # Reasonable limit
						self.options.tcpHostname.insert(self.options.tcpHostnameCursor, event.unicode)
						self.options.tcpHostnameCursor += 1
				elif hasattr(self.options, 'tcpPortActive') and self.options.tcpPortActive:
					if event.unicode.isdigit() and len(self.options.tcpPort) < 5:
						self.options.tcpPort.insert(self.options.tcpPortCursor, event.unicode)
						self.options.tcpPortCursor += 1
				elif hasattr(self.options, 'serialPortActive') and self.options.serialPortActive:
					if len(self.options.serialPort) < 50:
						self.options.serialPort.insert(self.options.serialPortCursor, event.unicode)
						self.options.serialPortCursor += 1
			else:
				self.options.addChar(event.unicode)
	def changeShipSize(self, increment: int):
		if self.gameStage == STAGES.PLACING and not self.grid.allShipsPlaced():
			self.grid.changeSize(increment)
			self.redrawNeeded = True
	def advanceAnimations(self):
		if self.gameStage in [STAGES.PLACING, STAGES.GAME_WAIT, STAGES.SHOOTING, STAGES.END_GRID_SHOW]:
			self.redrawNeeded |= pygame.display.get_active()
			Ship.advanceAnimations()
	def shoot(self, mousePos):
		if self.gameStage == STAGES.SHOOTING and not self.options.myGridShown and not self.transition:
			gridPos = self.opponentGrid.shoot(mousePos)
			if gridPos:
				self.shootReq(gridPos)
	def toggleGameReady(self):
		if self.gameStage in [STAGES.PLACING, STAGES.GAME_WAIT] and self.grid.allShipsPlaced():
			self.options.firstGameWait = False
			self.gameReadiness()
	def toggleRematch(self):
		if self.gameStage == STAGES.GAME_END and self.options.rematchPossible and self.session.connected:
			logging.debug(f'Rematch now {"in" * self.options.awaitingRematch}active')
			self.sendUpdateRematch(not self.options.awaitingRematch)
			self.redrawNeeded = True
	def updateTransition(self) -> int:
		if not self.transition: return 0.
		offset = self.transition.getGridOffset()
		if state := self.transition.update(offset):
			if state == 1:
				self.changeGridShown()
			else:
				self.transition = None
				self.redrawHUD()
				return 0.
		return offset

	# drawing --------------------------------
	def drawHUDMsg(self, text=None):
		if text is None: text = self.options.hudMsg
		msg_rect = Frontend.render(Frontend.FONT_ARIAL_MSGS, Constants.HUD_RECT.midbottom, text, (255, 255, 255), (40, 40, 40), (255, 255, 255), 2, 8, fitMode='midtop', border_bottom_left_radius=10, border_bottom_right_radius=10)
		Frontend.markDirty(msg_rect)
	def drawGame(self, transitionOffset):
		assert STAGES.COUNT == 12  # Added RADIO_CONNECTION
		if (not self.redrawNeeded and transitionOffset == 0) or self.gameStage == STAGES.CLOSING: return
		self.redrawNeeded = False
		drawHud = True
		if self.gameStage == STAGES.PLACING:
			self.grid.draw(flying=True)
			if self.options.hudMsg: self.drawHUDMsg()
		elif self.gameStage == STAGES.GAME_WAIT:
			self.grid.draw()
			self.drawHUDMsg(f" Waiting for opponent.{'.' * Ship.animationStage:<2}")
		elif self.gameStage in [STAGES.SHOOTING, STAGES.END_GRID_SHOW]:
			[self.opponentGrid, self.grid][self.options.myGridShown].draw(shots=True, offset=transitionOffset)
			if transitionOffset: self.transition.draw(transitionOffset)
		else:
			drawHud = False
			self.drawStatic()
		Frontend.drawHeader()
		if drawHud:
			border_rect = pygame.Rect(0, Constants.HEADER_HEIGHT, Constants.SCREEN_WIDTH, Constants.SCREEN_HEIGHT - Constants.HEADER_HEIGHT)
			pygame.draw.lines(Frontend.Runtime.display, (255, 255, 255), False, [(0, Constants.HEADER_HEIGHT), (0, Constants.SCREEN_HEIGHT-1), (Constants.SCREEN_WIDTH-1, Constants.SCREEN_HEIGHT-1), (Constants.SCREEN_WIDTH-1, Constants.HEADER_HEIGHT)])
			Frontend.markDirty(border_rect)
			Frontend.drawHUD()
		Frontend.update()
	def drawStatic(self):
		assert STAGES.COUNT == 12  # Added RADIO_CONNECTION
		full_screen_rect = pygame.Rect(0, 0, Constants.SCREEN_WIDTH, Constants.SCREEN_HEIGHT)
		
		Frontend.fillColor((255, 255, 255))
		if self.gameStage == STAGES.RADIO_CONNECTION:
			self._drawRadioConnectionMenu()
		elif self.gameStage == STAGES.MAIN_MENU:
			# For MAIN_MENU, use cached surface if available and nothing changed
			if Frontend.Runtime._staticMenuCache is not None:
				Frontend.Runtime.display.blit(Frontend.Runtime._staticMenuCache, (0, Constants.HEADER_HEIGHT))
			else:
				# Render and cache the main menu
				Frontend.render(Frontend.FONT_ARIAL_BIG, (150, 300), 'MAIN MENU')
				Frontend.render(Frontend.FONT_ARIAL_SMALL, (150, 400), 'Press ENTER to play multiplayer')
				# Cache the main menu surface for reuse (copy the area below header)
				menu_area = pygame.Rect(0, Constants.HEADER_HEIGHT, Constants.SCREEN_WIDTH, Constants.SCREEN_HEIGHT - Constants.HEADER_HEIGHT)
				Frontend.Runtime._staticMenuCache = Frontend.Runtime.display.subsurface(menu_area).copy()
		elif self.gameStage == STAGES.MULTIPLAYER_MENU:
			Frontend.render(Frontend.FONT_ARIAL_BIG, (150, 150), 'MULTIPLAYER')
			Frontend.render(Frontend.FONT_ARIAL_MIDDLE, (150, 250), 'Input your name')
			Frontend.render(Frontend.FONT_ARIAL_MIDDLE, Constants.MULTIPLAYER_INPUT_BOX, self.options.showedPlayerName(), (0, 0, 0), (255, 255, 255) if self.options.inputActive else (128, 128, 128), (0, 0, 0), 3, 8)
			Frontend.render(Frontend.FONT_ARIAL_SMALL, (150, 450), 'Press ENTER to play...')
			Frontend.Runtime._staticMenuCache = None  # Clear cache for dynamic menu
		elif self.gameStage == STAGES.PAIRING:
			Frontend.render(Frontend.FONT_ARIAL_BIG, (50, 300), 'Waiting for opponent...')
			Frontend.Runtime._staticMenuCache = None  # Clear cache
		elif self.gameStage == STAGES.GAME_END:
			Frontend.render(Frontend.FONT_ARIAL_BIG, (80, 200), self.options.gameEndMsg, (0, 0, 0))
			Frontend.render(Frontend.FONT_ARIAL_SMALL, (80, 300), 'Press enter to exit')
			self.grid.drawThumbnail(self.options.submittedPlayerName())
			self.opponentGrid.drawThumbnail(self.options.opponentName)
			img = Frontend.IMG_REMATCH[self.options.awaitingRematch + 2 * self.options.opponentRematching]
			if not self.options.rematchPossible: img = Frontend.IMG_REMATCH[-1]
			Frontend.blit(img, Constants.REMATCH_BTN_RECT)
			Frontend.Runtime._staticMenuCache = None  # Clear cache
		Frontend.markDirty(full_screen_rect)
	def _drawRadioConnectionMenu(self):
		'''Draw the radio connection GUI'''
		mouse_pos = pygame.mouse.get_pos()
		# Use actual window size for dynamic sizing
		screen_width = Frontend.Runtime.display.get_width()
		screen_height = Frontend.Runtime.display.get_height()
		
		# Title - centered, using larger font
		title_y = 80
		title_rect = Frontend.render(Frontend.FONT_ARIAL_RADIO_LARGE, (screen_width // 2, title_y), 'RADIO CONNECTION', (0, 0, 0), fitMode='center')
		
		# Connection type selection - styled as tabs, using larger font
		section_y = title_y + 80
		Frontend.render(Frontend.FONT_ARIAL_RADIO_MEDIUM, (screen_width // 2, section_y), 'Connection Type', (0, 0, 0), fitMode='center')
		
		conn_types = ['BLE', 'TCP', 'Serial']
		conn_y = section_y + 50
		conn_button_width = 160  # Increased from 140
		conn_button_height = 55  # Increased from 45
		conn_spacing = 20
		total_width = len(conn_types) * conn_button_width + (len(conn_types) - 1) * conn_spacing
		conn_start_x = (screen_width - total_width) // 2
		
		for i, conn_type in enumerate(conn_types):
			is_selected = self.options.radioConnectionType == conn_type
			is_hovered = False
			rect = Rect(conn_start_x + i * (conn_button_width + conn_spacing), conn_y, conn_button_width, conn_button_height)
			if rect.collidepoint(mouse_pos):
				is_hovered = True
			
			# Button styling
			if is_selected:
				bg_color = (100, 150, 255)  # Blue for selected
				text_color = (255, 255, 255)
				border_color = (50, 100, 200)
			elif is_hovered:
				bg_color = (220, 220, 255)  # Light blue for hover
				text_color = (0, 0, 0)
				border_color = (150, 150, 200)
			else:
				bg_color = (240, 240, 240)  # Light grey for unselected
				text_color = (100, 100, 100)
				border_color = (200, 200, 200)
			
			# Draw button with rounded corners effect (using thicker border)
			pygame.draw.rect(Frontend.Runtime.display, bg_color, rect, 0)
			pygame.draw.rect(Frontend.Runtime.display, border_color, rect, 3)
			Frontend.render(Frontend.FONT_ARIAL_RADIO_MEDIUM, rect, conn_type, text_color, fitMode='center')
		
		# Connection-specific UI
		content_y = conn_y + conn_button_height + 40
		if self.options.radioConnectionType == 'BLE':
			self._drawBLEConnectionUI(content_y, screen_width, screen_height)
		elif self.options.radioConnectionType == 'TCP':
			self._drawTCPConnectionUI(content_y, screen_width, screen_height)
		elif self.options.radioConnectionType == 'Serial':
			self._drawSerialConnectionUI(content_y, screen_width, screen_height)
		
		# Status message - centered, with better styling and larger font
		status_y = screen_height - 140
		status_bg_rect = Rect(50, status_y - 10, screen_width - 100, 45)  # Increased height from 35 to 45
		status_color = (240, 240, 240)
		if 'failed' in self.options.connectionStatus.lower() or 'error' in self.options.connectionStatus.lower():
			status_color = (255, 240, 240)  # Light red for errors
		elif 'connected' in self.options.connectionStatus.lower() or 'found' in self.options.connectionStatus.lower():
			status_color = (240, 255, 240)  # Light green for success
		pygame.draw.rect(Frontend.Runtime.display, status_color, status_bg_rect, 0)
		pygame.draw.rect(Frontend.Runtime.display, (200, 200, 200), status_bg_rect, 2)
		Frontend.render(Frontend.FONT_ARIAL_RADIO_SMALL, status_bg_rect, f'Status: {self.options.connectionStatus}', (0, 0, 0), fitMode='center')
		
		# Action buttons - better positioned and styled, larger
		button_y = screen_height - 80
		button_width = 180  # Increased from 160
		button_height = 55  # Increased from 45
		button_spacing = 20
		
		# Connect button
		connect_rect = Rect((screen_width - button_width * 2 - button_spacing) // 2, button_y, button_width, button_height)
		is_connect_hovered = connect_rect.collidepoint(mouse_pos)
		connect_bg = (100, 255, 100) if is_connect_hovered else (150, 255, 150)
		Frontend.render(Frontend.FONT_ARIAL_RADIO_MEDIUM, connect_rect, 'CONNECT', (0, 0, 0), connect_bg, (50, 200, 50), 3, fitMode='center')
		
		# Refresh button (for BLE)
		if self.options.radioConnectionType == 'BLE':
			refresh_rect = Rect(connect_rect.right + button_spacing, button_y, button_width, button_height)
			is_refresh_hovered = refresh_rect.collidepoint(mouse_pos)
			refresh_bg = (100, 150, 255) if is_refresh_hovered else (150, 200, 255)
			Frontend.render(Frontend.FONT_ARIAL_RADIO_MEDIUM, refresh_rect, 'REFRESH', (0, 0, 0), refresh_bg, (50, 100, 200), 3, fitMode='center')
		
		# Back button - centered below other buttons
		back_y = button_y + button_height + 15
		back_rect = Rect((screen_width - button_width) // 2, back_y, button_width, button_height)
		is_back_hovered = back_rect.collidepoint(mouse_pos)
		back_bg = (255, 150, 150) if is_back_hovered else (255, 200, 200)
		Frontend.render(Frontend.FONT_ARIAL_RADIO_MEDIUM, back_rect, 'BACK', (0, 0, 0), back_bg, (200, 50, 50), 3, fitMode='center')
		
		Frontend.Runtime._staticMenuCache = None
	
	def _drawBLEConnectionUI(self, start_y, screen_width, screen_height):
		'''Draw BLE device selection UI'''
		mouse_pos = pygame.mouse.get_pos()
		
		# Section header - larger font
		header_y = start_y
		Frontend.render(Frontend.FONT_ARIAL_RADIO_MEDIUM, (screen_width // 2, header_y), 'Available Devices', (0, 0, 0), fitMode='center')
		
		# Device list container - taller and uses dynamic width
		list_y = header_y + 50
		list_height = min(300, screen_height - list_y - 200)  # Dynamic height, max 300
		list_rect = Rect(50, list_y, screen_width - 100, list_height)
		pygame.draw.rect(Frontend.Runtime.display, (250, 250, 250), list_rect, 0)
		pygame.draw.rect(Frontend.Runtime.display, (200, 200, 200), list_rect, 2)
		
		if not self.options.bleDevices:
			# Empty state message - larger font
			empty_msg_rect = Rect(list_rect.x + 20, list_rect.y + list_rect.height // 2 - 20, list_rect.width - 40, 40)
			Frontend.render(Frontend.FONT_ARIAL_RADIO_SMALL, empty_msg_rect, 'No devices found. Click Refresh to scan.', (150, 150, 150), fitMode='center')
		else:
			# Device list with scrolling - taller items
			device_item_height = 50  # Increased from 35
			max_visible = min(list_rect.height // device_item_height, len(self.options.bleDevices))
			start_idx = max(0, min(self.options.selectedDeviceIndex - 2, len(self.options.bleDevices) - max_visible))
			end_idx = min(len(self.options.bleDevices), start_idx + max_visible)
			
			for i in range(start_idx, end_idx):
				device = self.options.bleDevices[i]
				device_y = list_rect.y + 10 + (i - start_idx) * device_item_height
				device_rect = Rect(list_rect.x + 10, device_y, list_rect.width - 20, device_item_height - 5)
				
				is_selected = i == self.options.selectedDeviceIndex
				is_hovered = device_rect.collidepoint(mouse_pos)
				
				if is_selected:
					bg_color = (180, 200, 255)
					text_color = (0, 0, 0)
					border_color = (100, 150, 255)
				elif is_hovered:
					bg_color = (240, 240, 255)
					text_color = (0, 0, 0)
					border_color = (200, 200, 255)
				else:
					bg_color = (255, 255, 255)
					text_color = (100, 100, 100)
					border_color = (220, 220, 220)
				
				pygame.draw.rect(Frontend.Runtime.display, bg_color, device_rect, 0)
				pygame.draw.rect(Frontend.Runtime.display, border_color, device_rect, 2)
				
				# Don't truncate - show full device info with larger font
				device_name = device.get('name', 'Unknown')
				device_addr = device.get('address', 'N/A')
				display_text = f"{device_name} ({device_addr})"
				
				Frontend.render(Frontend.FONT_ARIAL_RADIO_SMALL, device_rect, display_text, text_color, fitMode='midleft', boundaryPadding=12)
	
	def _drawTCPConnectionUI(self, start_y, screen_width, screen_height):
		'''Draw TCP connection input UI'''
		# Hostname input - larger font
		label_y = start_y
		Frontend.render(Frontend.FONT_ARIAL_RADIO_MEDIUM, (screen_width // 2, label_y), 'Hostname', (0, 0, 0), fitMode='center')
		
		hostname_box = Rect(screen_width // 2 - 200, label_y + 50, 400, 55)  # Increased height from 45
		hostname_text = ''.join(self.options.tcpHostname)
		is_active = hasattr(self.options, 'tcpHostnameActive') and self.options.tcpHostnameActive
		if is_active:
			hostname_text = hostname_text[:self.options.tcpHostnameCursor] + '|' + hostname_text[self.options.tcpHostnameCursor:]
		
		bg_color = (255, 255, 255) if is_active else (245, 245, 245)
		border_color = (100, 150, 255) if is_active else (200, 200, 200)
		pygame.draw.rect(Frontend.Runtime.display, bg_color, hostname_box, 0)
		pygame.draw.rect(Frontend.Runtime.display, border_color, hostname_box, 3)
		Frontend.render(Frontend.FONT_ARIAL_RADIO_SMALL, hostname_box, hostname_text or 'localhost', (0, 0, 0), fitMode='midleft', boundaryPadding=12)
		
		# Port input - larger font
		port_label_y = label_y + 120
		Frontend.render(Frontend.FONT_ARIAL_RADIO_MEDIUM, (screen_width // 2, port_label_y), 'Port', (0, 0, 0), fitMode='center')
		
		port_box = Rect(screen_width // 2 - 100, port_label_y + 50, 200, 55)  # Increased height from 45
		port_text = ''.join(self.options.tcpPort)
		is_port_active = hasattr(self.options, 'tcpPortActive') and self.options.tcpPortActive
		if is_port_active:
			port_text = port_text[:self.options.tcpPortCursor] + '|' + port_text[self.options.tcpPortCursor:]
		
		port_bg_color = (255, 255, 255) if is_port_active else (245, 245, 245)
		port_border_color = (100, 150, 255) if is_port_active else (200, 200, 200)
		pygame.draw.rect(Frontend.Runtime.display, port_bg_color, port_box, 0)
		pygame.draw.rect(Frontend.Runtime.display, port_border_color, port_box, 3)
		Frontend.render(Frontend.FONT_ARIAL_RADIO_SMALL, port_box, port_text or '5000', (0, 0, 0), fitMode='midleft', boundaryPadding=12)
	
	def _drawSerialConnectionUI(self, start_y, screen_width, screen_height):
		'''Draw Serial connection input UI'''
		# Serial port input - larger font
		label_y = start_y
		Frontend.render(Frontend.FONT_ARIAL_RADIO_MEDIUM, (screen_width // 2, label_y), 'Serial Port', (0, 0, 0), fitMode='center')
		
		port_box = Rect(screen_width // 2 - 200, label_y + 50, 400, 55)  # Increased height from 45
		port_text = ''.join(self.options.serialPort)
		is_active = hasattr(self.options, 'serialPortActive') and self.options.serialPortActive
		if is_active:
			port_text = port_text[:self.options.serialPortCursor] + '|' + port_text[self.options.serialPortCursor:]
		
		bg_color = (255, 255, 255) if is_active else (245, 245, 245)
		border_color = (100, 150, 255) if is_active else (200, 200, 200)
		pygame.draw.rect(Frontend.Runtime.display, bg_color, port_box, 0)
		pygame.draw.rect(Frontend.Runtime.display, border_color, port_box, 3)
		Frontend.render(Frontend.FONT_ARIAL_RADIO_SMALL, port_box, port_text or '/dev/ttyUSB0', (0, 0, 0), fitMode='midleft', boundaryPadding=12)
	
	def redrawHUD(self):
		grid = self.grid if self.options.myGridShown else self.opponentGrid
		Frontend.genHUD(self.options, grid.shipSizes, self.gameStage, not self.options.gameWon ^ self.options.myGridShown, bool(self.transition))
		self.redrawNeeded = True

class Transition:
	DURATION = 4000 # ms
	TRANSITION_WIDTH = Frontend.IMG_TRANSITION.get_width()
	GRID_WIDTH = Constants.SCREEN_WIDTH
	def __init__(self, toMyGrid: bool):
		self.direction = 1 if toMyGrid else -1
		self.firstHalf = True
		self.startTime = pygame.time.get_ticks()
	def __getRawOffset(self):
		x = (pygame.time.get_ticks() - self.startTime) / self.DURATION
		y = 6 * x ** 5 - 15 * x ** 4 + 10 * x ** 3
		y *= self.TRANSITION_WIDTH + self.GRID_WIDTH
		return int(y * self.direction)
	def getGridOffset(self) -> int:
		off = self.__getRawOffset()
		if not self.firstHalf: off -= self.direction * (self.TRANSITION_WIDTH + self.GRID_WIDTH)
		return off
	def update(self, offset) -> int:
		if self.firstHalf and abs(offset) > self.GRID_WIDTH:
			self.firstHalf = False
			return 1
		if not self.firstHalf and offset * self.direction >= 0: return 2
		return 0
	def draw(self, offset):
		gridOnLeft = (self.direction == 1) ^ self.firstHalf
		offset += gridOnLeft * self.GRID_WIDTH
		transition_rect = Frontend.blit(Frontend.IMG_TRANSITION, (offset, Constants.SCREEN_HEIGHT), rectAttr='bottomleft' if gridOnLeft else 'bottomright')
		Frontend.markDirty(transition_rect)

class Options:
	'''Class responsible for loading, holding and storing client side options,
		such as settings and stored data'''
	MAX_LEN = 19
	def __init__(self):
		self.playerName: list[str] = []
		self.repeatableInit()
	def repeatableInit(self):
		self.cursor:int = len(self.playerName) # points before char
		self.inputActive = False
		self.opponentName = ''

		self.firstGameWait = True
		self.opponentReady = False
		self.hudMsg = ''

		self.myGridShown = True
		self.gameWon = True
		self.gameEndMsg = 'UNREACHABLE!'

		self.awaitingRematch = False
		self.rematchPossible = True
		self.opponentRematching = False

	def addChar(self, c):
		if c == ' ': c = '_'
		if c and (c in string.ascii_letters or c in string.digits or c in '!#*+-_'):
			if self.inputActive:
				if len(self.playerName) < Options.MAX_LEN:
					self.playerName.insert(self.cursor, c)
					self.cursor += 1
	def removeChar(self, delAfter=False):
		if (self.cursor <= 0 and not delAfter) or (self.cursor == len(self.playerName) and delAfter): return
		if self.inputActive and len(self.playerName):
			if not delAfter: self.cursor -= 1
			self.playerName.pop(self.cursor)
	def moveCursor(self, off):
		if 0 <= self.cursor + off <= len(self.playerName):
			self.cursor += off
	def mouseClick(self, mousePos) -> bool:
		if Constants.MULTIPLAYER_INPUT_BOX.collidepoint(mousePos) ^ self.inputActive:
			self.inputActive ^= True
			self.cursor = len(self.playerName)
			return True
	def showedPlayerName(self) -> str:
		if not self.playerName and not self.inputActive: return 'Name'
		s = ''.join(self.playerName)
		if self.inputActive: s = s[:self.cursor] + '|' + s[self.cursor:]
		return s
	def submittedPlayerName(self) -> str:
		if not self.playerName: return 'Noname'
		return ''.join(self.playerName)

Ship = TypeVar('Ship')
class Grid:
	def __init__(self, isLocal: bool):
		self.isLocal = isLocal
		self.initShipSizes()
		self.flyingShip: Ship = Ship([-1, -1], 0, True)
		self.ships: list[Ship] = []
		self.shots = [[SHOTS.NOT_SHOTTED] * Constants.GRID_WIDTH for y in range(Constants.GRID_HEIGHT)]
	def initShipSizes(self):
		self.shipSizes: dict[int, int] = {1: 2, 2: 4, 3: 2, 4: 1} # shipSize : shipCount

	def shipsDicts(self):
		return [ship.asDict() for ship in self.ships]
	def allShipsPlaced(self):
			return not any(self.shipSizes.values())

	# interface ---------------------------------------------
	def rotateShip(self): # TODO: maybe the one-ship shouldn't be turned?
		self.flyingShip.horizontal = not self.flyingShip.horizontal
	def changeCursor(self, mousePos):
		clicked = self._getClickedShip(mousePos)
		initialSize = self.flyingShip.size
		if clicked:
			self.changeSize(+1, canBeSame=True, currSize=clicked.size)
		if not clicked or (self.flyingShip.size == initialSize):
			self.removeShipInCursor()
	def removeShipInCursor(self):
		self.flyingShip.size = 0

	def mouseClick(self, mousePos, rightClick: bool) -> bool:
		'''handles the mouse click
		@rightClick - the click is considered RMB click, otherwise LMB
		@return - if anything changed'''
		if self.flyingShip.size == 0 or rightClick:
			return self.pickUpShip(mousePos)
		elif mousePos[1] >= Constants.GRID_Y_OFFSET:
			return self.placeShip()
		return False
	def canPlaceShip(self, placed):
		gridRect = Rect(0, 0, Constants.GRID_WIDTH, Constants.GRID_HEIGHT)
		if not gridRect.contains(placed.getOccupiedRect()):
			return False
		for ship in self.ships:
			if placed.isColliding(ship):
				return False
		return True

	def placeShip(self) -> bool:
		placed = self.flyingShip.getPlacedShip()
		canPlace = self.canPlaceShip(placed)
		if canPlace:
			self.ships.append(placed)
			self.shipSizes[placed.size] -= 1
			self.changeSize(+1, canBeSame=True)
		return canPlace
	def autoplace(self):
		if self.shipSizes == {1: 2, 2: 4, 3: 2, 4: 1}:
			self.flyingShip.setSize(0)
			dicts = [{'pos': [3, 0], 'size': 2, 'horizontal': True, 'hitted': [False, False]}, {'pos': [4, 3], 'size': 2, 'horizontal': False, 'hitted': [False, False]}, {'pos': [5, 7], 'size': 3, 'horizontal': True, 'hitted': [False, False, False]}, {'pos': [1, 5], 'size': 4, 'horizontal': False, 'hitted': [False, False, False, False]}, {'pos': [8, 4], 'size': 1, 'horizontal': True, 'hitted': [False]}, {'pos': [6, 1], 'size': 1, 'horizontal': False, 'hitted': [False]}, {'pos': [5, 9], 'size': 2, 'horizontal': True, 'hitted': [False, False]}, {'pos': [1, 1], 'size': 2, 'horizontal': False, 'hitted': [False, False]}, {'pos': [9, 0], 'size': 3, 'horizontal': False, 'hitted': [False, False, False]}]
			for d in dicts:
				ship = Ship.fromDict(d)
				self.ships.append(ship)
				self.shipSizes[ship.size] -= 1
			assert self.allShipsPlaced(), 'autoplace is expected to place all ships'
	def pickUpShip(self, mousePos) -> bool:
		ship = self._getClickedShip(mousePos)
		if ship:
			self.removeShipInCursor()
			self.flyingShip = ship.getFlying()
			self.ships.remove(ship)
			self.shipSizes[ship.size] += 1
		return bool(ship)
	def _getClickedShip(self, mousePos):
		for ship in self.ships:
			if ship.realRect.collidepoint(mousePos):
				return ship
		return None

	def _nextShipSize(self, startSize, increment):
		currSize = startSize + increment
		while currSize not in self.shipSizes:
			if currSize == startSize: break
			currSize += increment
			currSize = currSize % (max(self.shipSizes.keys()) + 1)
		return currSize
	def changeSize(self, increment: int, *, canBeSame=False, currSize=None):
		if currSize is None:
			currSize = self.flyingShip.size
		startSize = currSize
		if not canBeSame:
			currSize = self._nextShipSize(currSize, increment)
		while self.shipSizes[currSize] == 0:
			currSize = self._nextShipSize(currSize, increment)
			if currSize == startSize:
				if self.shipSizes[currSize] == 0:
					self.removeShipInCursor()
				return
		self.flyingShip.setSize(currSize)

	# shooting --------------------------------------------------
	def localGridShotted(self, pos, update=True) -> tuple[bool, Ship]:
		'''returns if hitted, any hitted ship'''
		for ship in self.ships:
			if ship.shot(pos, update):
				return True, ship
		return False, None
	def gotShotted(self, pos, hitted=False, sunkenShip=None):
		'''process shot result for opponents grid'''
		if self.isLocal: hitted, sunkenShip = self.localGridShotted(pos)
		else: assert self.shots[pos[1]][pos[0]] == SHOTS.SHOTTED_UNKNOWN
		self.shots[pos[1]][pos[0]] = [SHOTS.NOT_HITTED, SHOTS.HITTED][hitted]
		if sunkenShip and all(sunkenShip.hitted):
			if not self.isLocal: self.ships.append(sunkenShip)
			self.shipSizes[sunkenShip.size] -= 1
			self._markBlocked(sunkenShip)
	def shoot(self, mousePos) -> Optional[list[int]]:
		'''mouse click -> clicked grid pos if shooting location available'''
		if mousePos[1] < Constants.GRID_Y_OFFSET: return None
		clickedX, clickedY = mousePos[0] // Constants.GRID_X_SPACING, (mousePos[1] - Constants.GRID_Y_OFFSET) // Constants.GRID_Y_SPACING
		if self.shots[clickedY][clickedX] != SHOTS.NOT_SHOTTED: return None
		self.shots[clickedY][clickedX] = SHOTS.SHOTTED_UNKNOWN
		if self.isLocal: self.gotShotted((clickedX, clickedY))
		return [clickedX, clickedY]
	def _markBlocked(self, ship: Ship):
		'''marks squares around sunken ship'''
		rect: Rect = ship.getnoShipsRect()
		occupied: Rect = ship.getOccupiedRect()
		for x in range(rect.x, rect.x + rect.width):
			for y in range(rect.y, rect.y + rect.height):
				if self.shots[y][x] == SHOTS.NOT_SHOTTED:
					self.shots[y][x] = SHOTS.BLOCKED
				if occupied.collidepoint((x, y)):
					self.shots[y][x] = SHOTS.HITTED_SUNKEN
	def updateAfterGameEnd(self, dicts):
		assert not self.isLocal
		for ship in dicts['ships']:
			ship = Ship.fromDict(ship)
			if self.canPlaceShip(ship): self.ships.append(ship)
		for y, row in enumerate(self.shots):
			for x, shot in enumerate(row):
				if shot == SHOTS.HITTED: self.shots[y][x] = SHOTS.HITTED_SUNKEN

	# drawing -----------------------------------------------
	def drawShot(self, color, x, y, offset, *, thumbRect:Rect=None):
		pos = (x * Constants.GRID_X_SPACING + Constants.GRID_X_SPACING // 2 + offset, y * Constants.GRID_Y_SPACING + Constants.GRID_Y_SPACING // 2 + Constants.GRID_Y_OFFSET) if thumbRect is None else (thumbRect.x + Constants.THUMBNAIL_SPACINGS * x + Constants.THUMBNAIL_SPACINGS // 2 + 1, thumbRect.y + Constants.THUMBNAIL_SPACINGS * y + Constants.THUMBNAIL_SPACINGS // 2 + 1)
		Frontend.drawCircle(color, pos, (Constants.GRID_X_SPACING if thumbRect is None else Constants.THUMBNAIL_SPACINGS) // 4)
	def drawShots(self, offset=0, *, thumbRect:Rect=None):
		# Pre-compute colors dict once
		colors = {SHOTS.NOT_HITTED: (11, 243, 255)}
		if not self.isLocal: 
			colors.update({SHOTS.HITTED: (255, 0, 0), SHOTS.BLOCKED: (128, 128, 128)})
		if thumbRect is not None: 
			colors.update({SHOTS.HITTED: (255, 0, 0), SHOTS.HITTED_SUNKEN: (255, 0, 0), SHOTS.NOT_SHOTTED: (0, 0, 0)})
		
		# Optimize: cache localGridShotted for local grids to avoid repeated calls
		local_shotted_cache = {}
		if self.isLocal:
			for y, lineShotted in enumerate(self.shots):
				for x, shot in enumerate(lineShotted):
					if shot == SHOTS.NOT_SHOTTED:
						local_shotted_cache[(x, y)] = self.localGridShotted((x, y), update=False)[0]
		
		# Draw shots
		for y, lineShotted in enumerate(self.shots):
			for x, shot in enumerate(lineShotted):
				if shot in colors:
					# For local grids, check cache for NOT_SHOTTED
					if shot == SHOTS.NOT_SHOTTED and self.isLocal:
						if not local_shotted_cache.get((x, y), False):
							continue
					self.drawShot(colors[shot], x, y, offset, thumbRect=thumbRect)
	def draw(self, *, flying=False, shots=False, offset=0):
		Frontend.drawBackground(offset)
		grid_rect = pygame.Rect(offset, Constants.GRID_Y_OFFSET, Constants.SCREEN_WIDTH, Constants.SCREEN_HEIGHT - Constants.GRID_Y_OFFSET)
		for ship in self.ships: ship.draw(offset)
		if shots: self.drawShots(offset=offset)
		if flying and self.flyingShip.size: self.flyingShip.draw()
		Frontend.markDirty(grid_rect)
	def _drawThumbBackground(self, rect: pygame.Rect):
		Frontend.drawRect(rect, (0, 0, 255))
		for i in range(11):
			Frontend.drawLine((0, 0, 0), (rect.x, rect.y + Constants.THUMBNAIL_SPACINGS * i), (rect.right, rect.y + Constants.THUMBNAIL_SPACINGS * i))
			Frontend.drawLine((0, 0, 0), (rect.x + Constants.THUMBNAIL_SPACINGS * i, rect.y), (rect.x + Constants.THUMBNAIL_SPACINGS * i, rect.bottom))
	def _drawShipBodyLines(self, rect: Rect):
		for ship in self.ships:
			pos = rect.x + Constants.THUMBNAIL_SPACINGS // 2, rect.y + Constants.THUMBNAIL_SPACINGS // 2
			start = pos[0] + Constants.THUMBNAIL_SPACINGS * ship.pos[0], pos[1] + Constants.THUMBNAIL_SPACINGS * ship.pos[1]
			end = start[0] + Constants.THUMBNAIL_SPACINGS * (ship.widthInGrid - 1), start[1] + Constants.THUMBNAIL_SPACINGS * (ship.heightInGrid - 1)
			Frontend.drawLine((255, 0, 0) if all(ship.hitted) else (0, 0, 0), start, end, 4)
	def drawThumbnail(self, playerName):
		rect = Constants.THUMBNAIL_GRID_RECTS[not self.isLocal]
		Frontend.drawThumbnailName(not self.isLocal, playerName, rect)
		self._drawThumbBackground(rect)
		self._drawShipBodyLines(rect)
		self.drawShots(thumbRect=rect)


class Ship:
	animationStage = 0 # 0 - 2
	animationDirection = True
	def __init__(self, pos: list, size, horizontal, hitted=None):
		self.pos: list[int] = pos
		self.size: int = size
		self.horizontal: bool = horizontal
		if hitted is None:
			hitted = [False] * size
		self.hitted: list[bool] = hitted

	def asDict(self):
		return {'pos': self.pos, 'size': self.size, 'horizontal': self.horizontal, 'hitted': self.hitted}
	@ classmethod
	def fromDict(self, d: dict):
		if d is None: return None
		return Ship(d['pos'], d['size'], d['horizontal'], d['hitted'])

	def setSize(self, size):
		self.size = size
		self.hitted = [False] * size
	def getFlying(self):
		return Ship([-1, -1], self.size, self.horizontal)
	def getPlacedShip(self):
		assert self.pos == [-1, -1], 'only ship which is flying can be placed'
		realX, realY = self.realPos
		x = realX // Constants.GRID_X_SPACING
		x += (realX % Constants.GRID_X_SPACING) > (Constants.GRID_X_SPACING // 2)
		y = realY // Constants.GRID_Y_SPACING
		y += (realY % Constants.GRID_Y_SPACING) > (Constants.GRID_Y_SPACING // 2)
		return Ship([x, y], self.size, self.horizontal)

	@ property
	def widthInGrid(self):
		return (self.size - 1) * self.horizontal + 1
	@ property
	def heightInGrid(self):
		return (self.size - 1) * (not self.horizontal) + 1
	@ property
	def realPos(self) -> list[int]:
		'''return real pos wrt grid'''
		if self.pos == [-1, -1]:
			mouseX, mouseY = mouse.get_pos()
			return [mouseX - self.widthInGrid * Constants.GRID_X_SPACING // 2, mouseY - Constants.GRID_Y_OFFSET - self.heightInGrid * Constants.GRID_Y_SPACING // 2]
		else:
			return [self.pos[0] * Constants.GRID_X_SPACING, self.pos[1] * Constants.GRID_Y_SPACING]
	@ property
	def realRect(self):
		'''Rect of window ship coordinates'''
		return Rect(self.realPos[0], self.realPos[1] + Constants.GRID_Y_OFFSET, self.widthInGrid * Constants.GRID_X_SPACING, self.heightInGrid * Constants.GRID_Y_SPACING)

	def getRealSegmentCoords(self):
		'''returns list of real coords of all ship segments'''
		segments = []
		realX, realY = self.realPos
		for i in range(self.size):
			segments.append([realX, realY])
			realX += Constants.GRID_X_SPACING * self.horizontal
			realY += Constants.GRID_Y_SPACING * (not self.horizontal)
		return segments

	def getnoShipsRect(self):
		rect = Rect(self.pos[0] - 1, self.pos[1] - 1, self.widthInGrid + 2, self.heightInGrid + 2)
		return rect.clip(Rect(0, 0, Constants.GRID_WIDTH, Constants.GRID_HEIGHT))
	def getOccupiedRect(self):
		return Rect(self.pos[0], self.pos[1], self.widthInGrid, self.heightInGrid)
	def isColliding(self, other):
		'''checks if other collides with the noShipsRect of self'''
		return self.getnoShipsRect().colliderect(other.getOccupiedRect())

	def shot(self, pos, update) -> bool:
		if not self.getOccupiedRect().collidepoint(pos):
			return False

		realPos = pos[0] * Constants.GRID_X_SPACING, pos[1] * Constants.GRID_Y_SPACING
		for i, (x, y) in enumerate(self.getRealSegmentCoords()):
			r = Rect(x, y, 1, 1)
			if r.collidepoint((realPos)):
				if update: self.hitted[i] = True
				return True
		return False
	@ classmethod
	def advanceAnimations(cls):
		cls.animationStage += cls.animationDirection * 2 - 1
		if cls.animationStage == 0:
			cls.animationDirection = True
		elif cls.animationStage == 2:
			cls.animationDirection = False
	def draw(self, offset=0):
		img = Frontend.getFrame(self.size, self.horizontal, self.hitted, self.animationStage)
		rect = img.get_rect()
		rect.center = self.realRect.center
		rect.x += offset
		Frontend.blit(img, rect)
