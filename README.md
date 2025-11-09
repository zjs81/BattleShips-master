# BattleShips - MeshCore Edition

## About
This is a fork of [Michal-Martinek/BattleShips](https://github.com/Michal-Martinek/BattleShips), modified to work over the MeshCore network instead of traditional TCP/IP sockets.

BattleShips is a copy of the pen and pencil game called BattleShips, in Czech known as 'Lodě'. This fork has been adapted to use peer-to-peer communication via MeshCore, enabling off-grid mesh networking gameplay.

**Original Repository:** https://github.com/Michal-Martinek/BattleShips

This is a picture of what the game looks like right now:
![Screenshot](Screenshot.png)

## Key Changes from Original
- **Peer-to-Peer Architecture**: Removed the central server; players communicate directly via MeshCore
- **MeshCore Integration**: Uses `meshcore-cli` for radio communication (BLE, TCP, or Serial)
- **Radio Connection GUI**: Added a user interface for selecting and connecting to MeshCore devices
- **Message Chunking**: Implements automatic message chunking for MeshCore's 100-byte message size limit

## Requirements
- Python 3.9 or compatible
- pygame (>=2.0.0)
- meshcore-cli (>=0.1.0)
- A MeshCore-compatible radio device

Install dependencies:
```bash
pip install -r requirements.txt
```

Or install manually:
```bash
pip install pygame
pip install meshcore-cli
# Or using pipx (recommended for meshcore-cli):
pipx install meshcore-cli
pip install pygame
```

## Running
This version uses peer-to-peer communication, so **no server is needed**. Simply run the game on each device:

```bash
python BattleShips.py
```

### First-Time Setup
1. Start the game and enter multiplayer mode
2. Enter your player name
3. Select your radio connection method:
   - **BLE**: Scan and select a Bluetooth Low Energy device
   - **TCP**: Enter hostname and port for TCP/IP connection
   - **Serial**: Enter serial port path (e.g., `/dev/ttyUSB0`)
4. Connect to your MeshCore device
5. The game will automatically discover and pair with available opponents

### Network Requirements
- Players must be connected to the same MeshCore mesh network
- For BLE: Devices must be paired and within range
- For TCP: Players must be on the same network or have network connectivity
- For Serial: Direct serial connection to MeshCore device

## Controls
#### LMB
Place a ship on the board or pick up a ship from the board.
#### RMB
Pick up a ship from the board. Note that this will override the ship you're currently holding.
#### Mouse wheel
Change size of the ship you're placing.
#### R
Change the orientation of the ship you're placing.
#### Q
Choose a ship which is the same size as the ship you are hovering over or free your cursor.
#### G
Change your state from waiting for opponnent to placing ships or vice versa.
Note that once you place all ships in your inventory you will be considered waiting for your opponent and you won't be able to move your ships around.
