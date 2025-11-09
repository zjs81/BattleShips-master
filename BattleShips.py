import os
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
import pygame
import logging
from Client import Game, Constants
from Shared.Enums import STAGES
from Shared.Helpers import runFuncLogged, initLogging
from Client import Frontend

def game():
	initLogging('client_log.txt')
	pygame.fastevent.init()
	pygame.time.set_timer(pygame.event.Event(pygame.USEREVENT), Constants.ANIMATION_TIMING)

	game = Game.Game()

	clockObj = pygame.time.Clock()
	while game.gameStage != STAGES.CLOSING:
		# Process events - always handle these even when window doesn't have focus
		events = pygame.fastevent.get()
		for event in events:
			if event.type == pygame.QUIT:
				game.quit()
			elif event.type == pygame.USEREVENT:
				# Only advance animations if window has focus (saves CPU when minimized)
				if Frontend.Runtime.windowHasFocus:
					game.advanceAnimations()
			elif event.type == pygame.KEYDOWN:
				assert STAGES.COUNT == 12  # Added RADIO_CONNECTION
				if game.gameStage in [STAGES.MAIN_MENU, STAGES.MULTIPLAYER_MENU, STAGES.RADIO_CONNECTION, STAGES.GAME_END, STAGES.END_GRID_SHOW]:
					game.keydownInMenu(event)
				elif event.key == pygame.K_r:
					game.rotateShip()
				elif event.key == pygame.K_q:
					game.changeCursor()
				elif event.key == pygame.K_g:
					game.toggleGameReady()
			elif event.type == pygame.MOUSEBUTTONDOWN:
				if event.button == 1:
					game.mouseClick(event.pos)
				elif event.button == 3:
					game.mouseClick(event.pos, rightClick=True)
				elif event.button == 4: # scroll up
					game.changeShipSize(+1)
				elif event.button == 5: # scroll down
					game.changeShipSize(-1)
			elif event.type == pygame.MOUSEBUTTONUP:
				if event.button == 1:
					Frontend.Runtime.windowGrabbedPos = None
			elif event.type == pygame.MOUSEMOTION:
				# Always process mouse movement for window dragging, but skip other processing if no focus
				if Frontend.Runtime.windowGrabbedPos or Frontend.Runtime.windowHasFocus:
					game.mouseMovement(event)
			elif event.type in [pygame.WINDOWFOCUSGAINED, pygame.WINDOWFOCUSLOST, pygame.WINDOWRESTORED]:
				Frontend.Runtime.windowHasFocus = event.type != pygame.WINDOWFOCUSLOST
				if not Frontend.Runtime.windowHasFocus: game.options.inputActive = False
				game.redrawNeeded = True

		# Skip network operations for static menus (no connection needed)
		# Skip drawing during window drag to reduce lag
		if Frontend.Runtime.windowGrabbedPos:
			# During window drag, skip all drawing and most processing to reduce lag
			pass  # Window movement is handled in mouseMovement, skip everything else
		elif Frontend.Runtime.windowHasFocus or game.gameStage == STAGES.CLOSING:
			# Handle connections for non-static menus
			if game.gameStage not in [STAGES.MAIN_MENU, STAGES.MULTIPLAYER_MENU]:
				game.handleConnections()
			transitionOffset = game.updateTransition()
			game.drawGame(transitionOffset)
		else:
			# Still update transition timing even when not focused, but don't draw
			game.updateTransition()
			# Sleep briefly when not focused to reduce CPU usage
			pygame.time.wait(50)
		
		# Reduce FPS for static menus to save CPU
		if game.gameStage in [STAGES.MAIN_MENU, STAGES.MULTIPLAYER_MENU, STAGES.RADIO_CONNECTION, STAGES.PAIRING]:
			clockObj.tick(30)  # Lower FPS for static menus
		else:
			clockObj.tick(Constants.FPS)

def main():
	runFuncLogged(game)
	Frontend.quit()
if __name__ == '__main__':
	main()
