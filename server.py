# server.py
try:
    import socketio
except ImportError:
    print("Error: socketio module not found. Please install it using:")
    print("pip install python-socketio aiohttp")
    import sys
    sys.exit(1)

from aiohttp import web
import uuid
import time
import asyncio

# Konfiguracja serwera Socket.IO z rozszerzonymi opcjami
sio = socketio.AsyncServer(
    cors_allowed_origins='*',  # Zezwalaj na połączenia z dowolnego źródła
    async_mode='aiohttp',      # Używaj aiohttp jako backendu
    logger=True,               # Włącz logowanie dla łatwiejszego debugowania
    engineio_logger=True,      # Włącz logowanie na poziomie Engine.IO
    ping_timeout=60,           # Dłuższy timeout dla pingów
    ping_interval=25,          # Częstsze pingi dla utrzymania połączenia
    max_http_buffer_size=1000000  # Większy bufor dla danych HTTP
)

# Utwórz aplikację aiohttp i podłącz do niej Socket.IO
app = web.Application()
sio.attach(app, socketio_path='socket.io')

# Game state
rooms = {}  # {room_id: {name, host_sid, players: {sid: {name, car_data}}, status}}
players = {}  # {sid: {name, room_id}}

@sio.event
async def connect(sid, environ, auth=None):
    print('Client connected:', sid)
    # Get player name from auth data if available
    player_name = f"Player_{sid[:4]}"
    if auth is not None:
        player_name = auth.get('player_name', player_name)
    
    players[sid] = {'name': player_name, 'room_id': None}
    print(f"Player {player_name} connected with sid {sid}")

@sio.event
async def disconnect(sid):
    print('Client disconnected:', sid)
    
    # Check if player was in a room
    if sid in players and players[sid]['room_id']:
        room_id = players[sid]['room_id']
        if room_id in rooms:
            # Get player data before removing
            player_name = "Unknown"
            is_racing = rooms[room_id]['status'] == 'racing'
            
            if sid in rooms[room_id]['players']:
                player_name = rooms[room_id]['players'][sid]['name']
                
                # Special handling for disconnection during race
                if is_racing:
                    print(f"Player {player_name} disconnected during race in room {room_id}")
                    
                    # Mark player as disconnected but don't remove immediately
                    # This allows them to potentially reconnect
                    rooms[room_id]['players'][sid]['disconnected'] = True
                    rooms[room_id]['players'][sid]['disconnect_time'] = time.time()
                    
                    # Store player data for potential reconnection
                    if 'disconnected_players' not in rooms[room_id]:
                        rooms[room_id]['disconnected_players'] = {}
                    
                    # Store player data for 60 seconds to allow reconnection
                    rooms[room_id]['disconnected_players'][player_name] = {
                        'sid': sid,
                        'data': rooms[room_id]['players'][sid],
                        'disconnect_time': time.time(),
                        'timeout': 60  # 60 seconds to reconnect
                    }
                    
                    # Notify other players about the disconnection
                    for player_sid in rooms[room_id]['players']:
                        if player_sid != sid:
                            await sio.emit('player_disconnected', {
                                'player_id': sid,
                                'player_name': player_name,
                                'can_reconnect': True,
                                'timeout': 60
                            }, to=player_sid)
                    
                    # Don't remove player immediately during race
                    # We'll handle cleanup in a background task
                    # But we'll still proceed with host transfer if needed
                else:
                    # Not racing, remove player immediately
                    del rooms[room_id]['players'][sid]
                    
                    # Notify other players
                    for player_sid in rooms[room_id]['players']:
                        await sio.emit('player_left', {'player_id': sid}, to=player_sid)
                
                # If host left, delete room or transfer host
                if sid == rooms[room_id]['host_sid']:
                    if rooms[room_id]['players']:
                        # Transfer host to first remaining player
                        new_host_sid = next(iter(rooms[room_id]['players']))
                        rooms[room_id]['host_sid'] = new_host_sid
                        rooms[room_id]['players'][new_host_sid]['is_host'] = True
                        
                        # Notify new host
                        await sio.emit('host_transferred', {}, to=new_host_sid)
                    else:
                        # Delete empty room
                        del rooms[room_id]
    
    # Remove player from players dict
    if sid in players:
        del players[sid]

@sio.event
async def get_rooms(sid, data=None):
    """Send list of available rooms to client"""
    room_list = []
    for room_id, room_data in rooms.items():
        # Only include rooms that are waiting (not racing)
        if room_data['status'] == 'waiting':
            room_list.append({
                'id': room_id,
                'name': room_data['name'],
                'player_count': len(room_data['players']),
                'status': room_data['status'],
                'max_players': 2  # 1v1 races
            })
    
    await sio.emit('rooms_list', {'rooms': room_list}, to=sid)

@sio.event
async def create_room(sid, data):
    """Create a new room"""
    room_name = data.get('room_name', f"Room {len(rooms) + 1}")
    room_id = str(uuid.uuid4())
    
    # Create room
    rooms[room_id] = {
        'name': room_name,
        'host_sid': sid,
        'players': {},
        'status': 'waiting',
        'created_at': time.time(),
        'map': 'map1'  # Default map
    }
    
    # Add player to room
    player_name = players[sid]['name']
    rooms[room_id]['players'][sid] = {
        'name': player_name,
        'car_data': None,
        'is_host': True,
        'ready': False
    }
    
    # Update player data
    players[sid]['room_id'] = room_id
    
    # Notify player they joined the room
    await sio.emit('room_joined', {
        'room_id': room_id,
        'room_name': room_name,
        'players': [
            {
                'id': sid,
                'name': player_name,
                'is_host': True,
                'car_name': None
            }
        ],
        'is_host': True
    }, to=sid)

@sio.event
async def join_room(sid, data):
    """Join an existing room"""
    room_id = data.get('room_id')
    
    if room_id not in rooms:
        await sio.emit('error', {'message': 'Room not found'}, to=sid)
        return
    
    room = rooms[room_id]
    
    # Check if room is full (1v1 = max 2 players)
    if len(room['players']) >= 2:
        await sio.emit('error', {'message': 'Room is full'}, to=sid)
        return
    
    # Check if room is already racing
    if room['status'] == 'racing':
        await sio.emit('error', {'message': 'Race already in progress'}, to=sid)
        return
    
    # Add player to room
    player_name = players[sid]['name']
    room['players'][sid] = {
        'name': player_name,
        'car_data': None,
        'is_host': False,
        'ready': False
    }
    
    # Update player data
    players[sid]['room_id'] = room_id
    
    # Prepare player list for response
    player_list = []
    for player_sid, player_data in room['players'].items():
        player_list.append({
            'id': player_sid,
            'name': player_data['name'],
            'is_host': player_data['is_host'],
            'car_name': player_data.get('car_data', {}).get('name') if player_data.get('car_data') else None
        })
    
    # Notify player they joined the room
    await sio.emit('room_joined', {
        'room_id': room_id,
        'room_name': room['name'],
        'players': player_list,
        'is_host': False
    }, to=sid)
    
    # Notify other players in the room
    for player_sid in room['players']:
        if player_sid != sid:
            await sio.emit('player_joined', {
                'player': {
                    'id': sid,
                    'name': player_name,
                    'is_host': False
                }
            }, to=player_sid)

@sio.event
async def leave_room(sid, data=None):
    """Leave the current room"""
    if sid in players and players[sid]['room_id']:
        room_id = players[sid]['room_id']
        
        if room_id in rooms:
            room = rooms[room_id]
            
            # Remove player from room
            if sid in room['players']:
                del room['players'][sid]
                
                # Notify other players
                for player_sid in room['players']:
                    await sio.emit('player_left', {'player_id': sid}, to=player_sid)
                
                # If host left, delete room or transfer host
                if sid == room['host_sid']:
                    if room['players']:
                        # Transfer host to first remaining player
                        new_host_sid = next(iter(room['players']))
                        room['host_sid'] = new_host_sid
                        room['players'][new_host_sid]['is_host'] = True
                        
                        # Notify new host
                        await sio.emit('host_transferred', {}, to=new_host_sid)
                    else:
                        # Delete empty room
                        del rooms[room_id]
            
            # Update player data
            players[sid]['room_id'] = None

@sio.event
async def select_car(sid, data):
    """Select a car for multiplayer"""
    if sid in players and players[sid]['room_id']:
        room_id = players[sid]['room_id']
        
        if room_id in rooms and sid in rooms[room_id]['players']:
            car_name = data.get('car_name', 'Unknown Car')
            car_index = data.get('car_index', 0)
            
            # Update player's car data
            rooms[room_id]['players'][sid]['car_data'] = {
                'name': car_name,
                'index': car_index
            }
            
            # Also store car_name directly for easier access
            rooms[room_id]['players'][sid]['car_name'] = car_name
            
            # Mark car as selected
            rooms[room_id]['players'][sid]['car_selected'] = True
            
            # Log car selection
            print(f"Player {players[sid]['name']} selected car: {car_name} (index: {car_index})")
            
            # Update room's last activity time
            rooms[room_id]['last_activity'] = time.time()
            
            # Notify other players about car selection
            for player_sid in rooms[room_id]['players']:
                if player_sid != sid:
                    await sio.emit('player_car_selected', {
                        'player_id': sid,
                        'car_name': car_name
                    }, to=player_sid)

@sio.event
async def player_ready(sid, data):
    """Handle player ready status"""
    if sid in players and players[sid]['room_id']:
        room_id = players[sid]['room_id']
        
        if room_id in rooms and sid in rooms[room_id]['players']:
            ready_status = data.get('ready', False)
            
            # Update player's ready status
            rooms[room_id]['players'][sid]['ready'] = ready_status
            
            # Update room's last activity time
            rooms[room_id]['last_activity'] = time.time()
            
            # Notify other players about ready status
            for player_sid in rooms[room_id]['players']:
                if player_sid != sid:
                    await sio.emit('player_ready_changed', {
                        'player_id': sid,
                        'ready': ready_status
                    }, to=player_sid)

@sio.event
async def chat_message(sid, data):
    """Send a chat message to all players in the room"""
    if sid in players and players[sid]['room_id']:
        room_id = players[sid]['room_id']
        
        if room_id in rooms:
            message = data.get('message', '')
            sender_name = rooms[room_id]['players'][sid]['name']
            
            # Update room's last activity time
            rooms[room_id]['last_activity'] = time.time()
            
            # Send message to all players in the room
            for player_sid in rooms[room_id]['players']:
                await sio.emit('chat_message', {
                    'sender': sender_name,
                    'message': message
                }, to=player_sid)

@sio.event
async def start_race(sid, data=None):
    """Start the race (any player can start if conditions are met)"""
    if sid in players and players[sid]['room_id']:
        room_id = players[sid]['room_id']
        
        if room_id in rooms:
            room = rooms[room_id]
            
            # Check if we have exactly 2 players for 1v1
            if len(room['players']) != 2:
                await sio.emit('error', {'message': 'Need exactly 2 players for a 1v1 race'}, to=sid)
                return
            
            # Check if player is ready (if not host)
            if room['host_sid'] != sid and not room['players'][sid].get('ready', False):
                await sio.emit('error', {'message': 'You must be ready to start the race'}, to=sid)
                return
            
            # Check if all other players are ready
            all_ready = True
            for player_sid, player_data in room['players'].items():
                if player_sid != sid and not player_data.get('ready', False):
                    all_ready = False
                    break
            
            if not all_ready:
                await sio.emit('error', {'message': 'All players must be ready to start the race'}, to=sid)
                return
                
            # Log who is starting the race
            print(f"Race being started by {players[sid]['name']} (sid: {sid})")
            print(f"Is host: {room['host_sid'] == sid}")
            print(f"Room players: {room['players']}")
            
            # Check if all players have selected a car
            # This check is now optional, as cars are automatically selected from profile
            # We'll just log a warning if some players haven't selected a car
            all_cars_selected = True
            for player_sid, player_data in room['players'].items():
                if not player_data.get('car_data'):
                    all_cars_selected = False
                    print(f"Warning: Player {player_data['name']} has not selected a car")
            
            # We don't block the race start anymore if cars aren't selected
            # The game client will handle default cars for players who haven't selected one
            
            # Update room status
            room['status'] = 'racing'
            
            # Notify all players that race is starting
            for player_sid in room['players']:
                # Include more data in race_starting event
                await sio.emit('race_starting', {
                    'map': room['map'],
                    'starter_sid': sid,
                    'starter_name': players[sid]['name'],
                    'players': {
                        p_sid: {
                            'name': p_data['name'],
                            'ready': p_data.get('ready', False),
                            'car_name': p_data.get('car_name', None),
                            'is_host': p_sid == room['host_sid']
                        }
                        for p_sid, p_data in room['players'].items()
                    }
                }, to=player_sid)
                
                # Log the event
                print(f"Sent race_starting event to {room['players'][player_sid]['name']}")
            
            # Start countdown after a short delay
            await asyncio.sleep(1)
            
            # Send countdown event
            for player_sid in room['players']:
                await sio.emit('race_countdown', {
                    'duration': 3
                }, to=player_sid)
            
            # Wait for countdown
            await asyncio.sleep(3)
            
            # Record race start time for anti-cheat
            room['race_start_time'] = time.time()
            
            # Prepare player data for race start
            players_data = {}
            for player_sid, player_data in room['players'].items():
                players_data[player_sid] = {
                    'player_name': player_data['name'],
                    'car_data': player_data.get('car_data', {}),
                    'car_name': player_data.get('car_name'),
                    'ready': player_data.get('ready', False),
                    'is_host': player_sid == room['host_sid']
                }
            
            # Send race start event to all players
            for player_sid in room['players']:
                await sio.emit('race_start', {
                    'player_id': player_sid,
                    'players': players_data,
                    'starter_sid': sid,
                    'starter_name': players[sid]['name'],
                    'timestamp': time.time()
                }, to=player_sid)
                
                # Log the event
                print(f"Sent race_start event to {room['players'][player_sid]['name']}")

@sio.event
async def player_update(sid, data):
    """Update player position during race"""
    if sid in players and players[sid]['room_id']:
        room_id = players[sid]['room_id']
        
        if room_id in rooms and rooms[room_id]['status'] == 'racing':
            # Validate data
            try:
                # Get position and speed with validation
                position = data.get('position', 0)
                speed = data.get('speed', 0)
                
                # Validate position (must be a number and within reasonable range)
                if not isinstance(position, (int, float)):
                    print(f"Warning: Invalid position type from {sid}: {type(position)}")
                    position = 0
                
                # Limit position to reasonable range (-1000 to 10000)
                position = max(-1000, min(10000, position))
                
                # Validate speed (must be a number and within reasonable range)
                if not isinstance(speed, (int, float)):
                    print(f"Warning: Invalid speed type from {sid}: {type(speed)}")
                    speed = 0
                
                # Limit speed to reasonable range (0 to 500)
                speed = max(0, min(500, speed))
                
                # Check for unrealistic movement (anti-cheat)
                if 'last_position' in rooms[room_id]['players'][sid]:
                    last_position = rooms[room_id]['players'][sid]['last_position']
                    last_update_time = rooms[room_id]['players'][sid]['last_update_time']
                    time_diff = time.time() - last_update_time
                    
                    # Calculate maximum possible movement based on time and reasonable max speed
                    max_movement = 300 * time_diff  # 300 units per second as max speed
                    actual_movement = abs(position - last_position)
                    
                    if actual_movement > max_movement and time_diff > 0.1:
                        print(f"Warning: Possible cheating detected for {sid}. Movement: {actual_movement}, Max allowed: {max_movement}")
                        # Limit the movement to the maximum allowed
                        if position > last_position:
                            position = last_position + max_movement
                        else:
                            position = last_position - max_movement
                
                # Store last position and update time for anti-cheat
                current_time = time.time()
                rooms[room_id]['players'][sid]['last_position'] = position
                rooms[room_id]['players'][sid]['last_update_time'] = current_time
                
                # Update room's last activity time
                rooms[room_id]['last_activity'] = current_time
                
                # Send update to other players
                for player_sid in rooms[room_id]['players']:
                    if player_sid != sid:
                        await sio.emit('player_update', {
                            'player_id': sid,
                            'position': position,
                            'speed': speed
                        }, to=player_sid)
            
            except Exception as e:
                print(f"Error processing player update: {e}")
                # Don't propagate invalid updates

@sio.event
async def join_race(sid, data):
    """Handle player joining a race directly (without room)"""
    player_name = data.get('player_name', players[sid]['name'] if sid in players else f"Player_{sid[:4]}")
    car_data = data.get('car_data', {})
    car_name = data.get('car_name', 'Default')
    is_host = data.get('is_host', False)
    
    print(f"Player {player_name} joining race with car: {car_name}, is_host: {is_host}")
    
    # Create a temporary room ID for this race
    room_id = f"race_{str(uuid.uuid4())[:8]}"
    
    # Create room if it doesn't exist (for the first player)
    if room_id not in rooms:
        rooms[room_id] = {
            'name': f"Race {len(rooms) + 1}",
            'host_sid': sid if is_host else None,
            'players': {},
            'status': 'waiting',
            'created_at': time.time(),
            'map': 'map1'  # Default map
        }
    
    # Add player to room
    rooms[room_id]['players'][sid] = {
        'name': player_name,
        'car_data': car_data,
        'car_name': car_name,
        'is_host': is_host,
        'ready': True  # Auto-ready in direct race join
    }
    
    # Update player data
    if sid in players:
        players[sid]['room_id'] = room_id
    else:
        players[sid] = {'name': player_name, 'room_id': room_id}
    
    # Acknowledge the join
    await sio.emit('race_joined', {
        'status': 'success',
        'room_id': room_id,
        'player_id': sid
    }, to=sid)
    
    # If this is a host player, start countdown after a short delay
    if is_host:
        print(f"Host player joined, starting countdown in room {room_id}")
        rooms[room_id]['status'] = 'racing'
        
        # Start countdown after a short delay
        await asyncio.sleep(1)
        
        # Send countdown event
        await sio.emit('race_countdown', {
            'duration': 3
        }, to=sid)
        
        # Wait for countdown
        await asyncio.sleep(3)
        
        # Prepare player data for race start
        players_data = {}
        for player_sid, player_data in rooms[room_id]['players'].items():
            players_data[player_sid] = {
                'player_name': player_data['name'],
                'car_data': player_data.get('car_data', {}),
                'car_name': player_data.get('car_name', 'Default')
            }
        
        # Send race start event
        await sio.emit('race_start', {
            'player_id': sid,
            'players': players_data
        }, to=sid)

@sio.event
async def player_ready_to_race(sid, data):
    """Handle player ready to race signal"""
    if sid in players and players[sid]['room_id']:
        room_id = players[sid]['room_id']
        if room_id in rooms:
            # Mark this player as ready to race
            if sid in rooms[room_id]['players']:
                rooms[room_id]['players'][sid]['race_ready'] = True
                
                # Check if all players are ready to race
                all_ready = all(player.get('race_ready', False) for player in rooms[room_id]['players'].values())
                
                if all_ready:
                    print(f"All players ready to race in room {room_id}")

@sio.event
async def player_finish(sid, data):
    """Handle player finishing the race"""
    if sid in players and players[sid]['room_id']:
        room_id = players[sid]['room_id']
        
        if room_id in rooms and rooms[room_id]['status'] == 'racing':
            finish_time = data.get('finish_time', 0)
            player_name = rooms[room_id]['players'][sid]['name']
            
            # Validate finish time
            if not isinstance(finish_time, (int, float)):
                print(f"Warning: Invalid finish time type from {player_name}: {type(finish_time)}")
                finish_time = 999.99  # Default to a high time
            
            # Check for unrealistic finish times (anti-cheat)
            race_start_time = rooms[room_id].get('race_start_time')
            current_time = time.time()
            
            if race_start_time:
                race_duration = current_time - race_start_time
                
                # If the race just started (less than 10 seconds ago), the finish time is suspicious
                if race_duration < 10 and finish_time < 10:
                    print(f"Warning: Possible cheating detected for {player_name}. Finished too quickly: {finish_time}s")
                    # Set a minimum finish time
                    finish_time = max(finish_time, 30.0)
            
            # Limit finish time to reasonable range (10 to 999.99 seconds)
            finish_time = max(10.0, min(999.99, finish_time))
            
            print(f"Player {player_name} finished race with time: {finish_time}")
            
            # Mark player as finished
            rooms[room_id]['players'][sid]['finished'] = True
            rooms[room_id]['players'][sid]['finish_time'] = finish_time
            
            # Update room's last activity time
            rooms[room_id]['last_activity'] = time.time()
            
            # Notify all players about this player finishing
            for player_sid in rooms[room_id]['players']:
                await sio.emit('player_finished', {
                    'player_id': sid,
                    'finish_time': finish_time
                }, to=player_sid)
            
            # Check if all players have finished
            all_finished = all(player.get('finished', False) for player in rooms[room_id]['players'].values())
            
            if all_finished:
                # Prepare results
                results = []
                for player_sid, player_data in rooms[room_id]['players'].items():
                    results.append({
                        'player_name': player_data['name'],
                        'finish_time': player_data['finish_time']
                    })
                
                # Sort by finish time
                results.sort(key=lambda x: x['finish_time'])
                
                print(f"Race results: {results}")
                
                # Send results to all players
                for player_sid in rooms[room_id]['players']:
                    await sio.emit('race_results', {
                        'results': results
                    }, to=player_sid)
                
                # Reset room status after race
                rooms[room_id]['status'] = 'waiting'
                
                # Reset player race data
                for player_sid in rooms[room_id]['players']:
                    if 'finished' in rooms[room_id]['players'][player_sid]:
                        del rooms[room_id]['players'][player_sid]['finished']
                    if 'finish_time' in rooms[room_id]['players'][player_sid]:
                        del rooms[room_id]['players'][player_sid]['finish_time']

# Background task to clean up inactive rooms
async def cleanup_inactive_rooms():
    """Periodically clean up inactive rooms and disconnected players"""
    while True:
        try:
            current_time = time.time()
            rooms_to_remove = []
            
            # Check all rooms for inactivity
            for room_id, room_data in rooms.items():
                # Check if room is empty
                if not room_data['players']:
                    rooms_to_remove.append(room_id)
                    continue
                
                # Check if room is inactive (no updates for 10 minutes)
                last_activity = room_data.get('last_activity', room_data.get('created_at', current_time))
                if current_time - last_activity > 600:  # 10 minutes
                    print(f"Removing inactive room: {room_id}")
                    rooms_to_remove.append(room_id)
                    continue
                
                # Check for disconnected players that have timed out
                if 'disconnected_players' in room_data:
                    players_to_remove = []
                    
                    for player_name, player_data in room_data['disconnected_players'].items():
                        disconnect_time = player_data.get('disconnect_time', 0)
                        timeout = player_data.get('timeout', 60)
                        
                        if current_time - disconnect_time > timeout:
                            players_to_remove.append(player_name)
                    
                    # Remove timed out players
                    for player_name in players_to_remove:
                        print(f"Removing timed out player {player_name} from room {room_id}")
                        del room_data['disconnected_players'][player_name]
                        
                        # Also remove from active players if still there
                        player_sid = room_data['disconnected_players'][player_name].get('sid')
                        if player_sid and player_sid in room_data['players']:
                            del room_data['players'][player_sid]
                            
                            # Notify other players
                            for sid in room_data['players']:
                                await sio.emit('player_left', {'player_id': player_sid}, to=sid)
            
            # Remove inactive rooms
            for room_id in rooms_to_remove:
                if room_id in rooms:
                    del rooms[room_id]
            
            # Log server stats
            print(f"Server stats: {len(rooms)} active rooms, {len(players)} connected players")
            
        except Exception as e:
            print(f"Error in cleanup task: {e}")
        
        # Run every 60 seconds
        await asyncio.sleep(60)

if __name__ == '__main__':
    print("\n=== XTRA CARS MULTIPLAYER SERVER ===")
    print("Uruchamianie serwera na porcie 5000...")
    print("Serwer będzie dostępny pod adresami:")
    print("- Lokalnie: http://127.0.0.1:5000")
    
    # Próba pobrania adresu IP w sieci lokalnej
    import socket
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        if local_ip != "127.0.0.1":
            print(f"- W sieci lokalnej: http://{local_ip}:8080")
    except:
        pass
    
    print("\nAby zatrzymać serwer, naciśnij Ctrl+C")
    print("=====================================\n")
    
    try:
        # Start background task for cleanup
        sio.start_background_task(cleanup_inactive_rooms)
        
        # Run the server
        web.run_app(app, port=8080)
    except KeyboardInterrupt:
        print("\nSerwer został zatrzymany.")
    except Exception as e:
        print(f"\nBłąd podczas uruchamiania serwera: {e}")
        print("\nMożliwe przyczyny:")
        print("1. Port 5000 jest już zajęty przez inną aplikację")
        print("2. Brak wymaganych bibliotek (socketio, aiohttp)")
        print("\nAby zainstalować wymagane biblioteki, wpisz:")
        print("pip install python-socketio aiohttp")
        input("\nNaciśnij Enter, aby zamknąć...")
