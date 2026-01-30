import pymem
import pymem.process
import struct
import win32api
import win32con
import win32gui
import win32ui
import cv2
import numpy as np
import time
import threading
from dataclasses import dataclass
from typing import List, Optional
import tkinter as tk
from tkinter import ttk

# ==================== OFFSET SCANNER ====================
class OffsetScanner:
    def __init__(self, pm):
        self.pm = pm
        self.base = pymem.process.module_from_name(
            pm.process_handle, "GeometryDash.exe"
        ).lpBaseOfDll
        
    def find_player_y(self):
        """Scan for player Y position (changing value)"""
        print("Move player up/down to find Y offset...")
        input("Press Enter when ready to scan...")
        
        # Get initial value
        initial_values = []
        for addr in range(self.base, self.base + 0x1000000, 4):
            try:
                val = self.pm.read_bytes(addr, 4)
                val = struct.unpack('f', val)[0]
                if 0 < val < 1000:  # Reasonable Y range
                    initial_values.append((addr, val))
            except:
                pass
        
        print(f"Found {len(initial_values)} potential addresses")
        input("Move player and press Enter to rescan...")
        
        # Find changed values
        for addr, initial_val in initial_values:
            try:
                new_val_bytes = self.pm.read_bytes(addr, 4)
                new_val = struct.unpack('f', new_val_bytes)[0]
                if abs(new_val - initial_val) > 10:  # Significant change
                    print(f"Found Y at: 0x{addr - self.base:X}")
                    return addr
            except:
                pass
        return None

# ==================== OVERLAY WINDOW ====================
class OverlayWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("GD Bot Overlay")
        self.root.attributes('-topmost', True)
        self.root.attributes('-transparentcolor', 'black')
        self.root.overrideredirect(True)
        self.root.geometry("300x200+10+10")
        
        # Make window click-through
        hwnd = self.root.winfo_id()
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, 
                              ex_style | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT)
        
        # Stats display
        self.stats_text = tk.StringVar()
        self.label = ttk.Label(self.root, textvariable=self.stats_text, 
                              font=("Consolas", 10), foreground="cyan", 
                              background="black")
        self.label.pack(expand=True, fill='both')
        
    def update_stats(self, player_x, player_y, objects, action):
        text = f"Player: ({player_x:.1f}, {player_y:.1f})\n"
        text += f"Obstacles ahead: {len(objects)}\n"
        text += f"Action: {action}\n"
        for i, obj in enumerate(objects[:3]):  # Show first 3 obstacles
            text += f"Obj {i}: ({obj['x']:.1f}, {obj['y']:.1f}) type={obj['type']}\n"
        self.stats_text.set(text)
        
    def run(self):
        self.root.after(100, self.update_loop)
        self.root.mainloop()
        
    def update_loop(self):
        # Updated by bot thread
        self.root.after(50, self.update_loop)

# ==================== MAIN BOT ====================
@dataclass
class GameObject:
    x: float
    y: float
    width: float
    type: int

class GeometryDashBot:
    def __init__(self, use_overlay=True):
        print("[*] Attaching to Geometry Dash...")
        self.pm = pymem.Pymem("GeometryDash.exe")
        self.base = pymem.process.module_from_name(
            self.pm.process_handle, "GeometryDash.exe"
        ).lpBaseOfDll
        
        # Offsets (GD 2.2 - update if needed)
        self.offsets = {
            'playlayer': 0x3222D0,
            'player_object': 0x164,
            'player_x': 0x64,
            'player_y': 0x68,
            'player_dead': 0x320,
            'objects_array': 0x2A0,
            'object_count': 0x2A4,
            'obj_x': 0x30,
            'obj_y': 0x34,
            'obj_width': 0x38,
            'obj_type': 0x3A0,
            'game_speed': 0x2E0
        }
        
        # Bot state
        self.running = False
        self.overlay = None
        self.current_action = "Waiting"
        
        if use_overlay:
            self.start_overlay()
            
        print("[+] Bot initialized!")
        
    def start_overlay(self):
        """Start overlay in separate thread"""
        def overlay_thread():
            self.overlay = OverlayWindow()
            self.overlay.run()
        
        threading.Thread(target=overlay_thread, daemon=True).start()
        time.sleep(1)  # Let overlay start
        
    # =========== MEMORY READING ===========
    def read_float(self, addr):
        try:
            return struct.unpack('f', self.pm.read_bytes(addr, 4))[0]
        except:
            return 0.0
    
    def read_int(self, addr):
        try:
            return struct.unpack('i', self.pm.read_bytes(addr, 4))[0]
        except:
            return 0
    
    def read_ptr(self, addr):
        try:
            return struct.unpack('I', self.pm.read_bytes(addr, 4))[0]
        except:
            return 0
    
    # =========== GAME DATA ===========
    def get_playlayer(self):
        return self.read_ptr(self.base + self.offsets['playlayer'])
    
    def get_player(self):
        playlayer = self.get_playlayer()
        if not playlayer:
            return 0
        return self.read_ptr(playlayer + self.offsets['player_object'])
    
    def get_player_x(self):
        player = self.get_player()
        if not player:
            return 0.0
        return self.read_float(player + self.offsets['player_x'])
    
    def get_player_y(self):
        player = self.get_player()
        if not player:
            return 0.0
        return self.read_float(player + self.offsets['player_y'])
    
    def is_dead(self):
        player = self.get_player()
        if not player:
            return True
        return self.read_int(player + self.offsets['player_dead']) == 1
    
    def get_game_speed(self):
        playlayer = self.get_playlayer()
        if not playlayer:
            return 1.0
        return self.read_float(playlayer + self.offsets['game_speed'])
    
    def get_objects(self, max_distance=500, limit=50):
        """Get obstacles within range"""
        objects = []
        playlayer = self.get_playlayer()
        if not playlayer:
            return objects
            
        player_x = self.get_player_x()
        obj_array = self.read_ptr(playlayer + self.offsets['objects_array'])
        obj_count = min(self.read_int(playlayer + self.offsets['object_count']), limit)
        
        for i in range(obj_count):
            obj_ptr = self.read_ptr(obj_array + i * 4)
            if not obj_ptr:
                continue
                
            x = self.read_float(obj_ptr + self.offsets['obj_x'])
            y = self.read_float(obj_ptr + self.offsets['obj_y'])
            width = self.read_float(obj_ptr + self.offsets['obj_width'])
            obj_type = self.read_int(obj_ptr + self.offsets['obj_type'])
            
            # Filter by distance
            if 0 < (x - player_x) < max_distance:
                obj = GameObject(x=x, y=y, width=width, type=obj_type)
                objects.append(obj)
        
        # Sort by distance
        objects.sort(key=lambda o: o.x)
        return objects
    
    # =========== BOT LOGIC ===========
    def should_jump(self, player_x, player_y, objects, game_speed):
        """Simple jump logic - can be replaced with ML"""
        if not objects:
            return False
            
        nearest = objects[0]
        distance = nearest.x - player_x
        
        # Jump timing based on speed
        jump_distance = 100 * game_speed
        
        if jump_distance < distance < jump_distance + 50:
            # Check if obstacle is above ground
            if nearest.y > 150:
                return True
            # Check if it's a low obstacle
            elif nearest.y < 100 and player_y > 200:
                return False  # Don't jump over low obstacles from high position
        
        return False
    
    def press_jump(self):
        """Simulate spacebar press"""
        win32api.keybd_event(0x20, 0, 0, 0)  # Space down
        time.sleep(0.02)
        win32api.keybd_event(0x20, 0, win32con.KEYEVENTF_KEYUP, 0)  # Space up
        self.current_action = "JUMP"
    
    def press_down(self):
        """Simulate down arrow (for ship/ufo)"""
        win32api.keybd_event(0x28, 0, 0, 0)  # Down down
        time.sleep(0.02)
        win32api.keybd_event(0x28, 0, win32con.KEYEVENTF_KEYUP, 0)
    
    # =========== MAIN LOOP ===========
    def run(self):
        print("[*] Starting bot loop...")
        print("[*] Switch to Geometry Dash window!")
        time.sleep(2)
        
        self.running = True
        death_count = 0
        
        try:
            while self.running:
                # Check if still in level
                if self.is_dead():
                    death_count += 1
                    print(f"[!] Died ({death_count}) - waiting for restart...")
                    self.current_action = "DEAD"
                    time.sleep(2)
                    continue
                
                # Get game state
                player_x = self.get_player_x()
                player_y = self.get_player_y()
                game_speed = self.get_game_speed()
                objects = self.get_objects(max_distance=400)
                
                # Decide action
                if self.should_jump(player_x, player_y, objects, game_speed):
                    self.press_jump()
                else:
                    self.current_action = "RUN"
                
                # Update overlay
                if self.overlay:
                    obj_dicts = [{'x': o.x, 'y': o.y, 'type': o.type} for o in objects[:5]]
                    self.overlay.update_stats(player_x, player_y, obj_dicts, self.current_action)
                
                # Small delay
                time.sleep(0.005)  # ~200 FPS update
                
        except KeyboardInterrupt:
            print("\n[*] Bot stopped by user")
        except Exception as e:
            print(f"[!] Error: {e}")
        finally:
            self.running = False
            
    def stop(self):
        self.running = False

# ==================== COMMAND LINE ====================
def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Geometry Dash Bot')
    parser.add_argument('--scan', action='store_true', help='Scan for offsets')
    parser.add_argument('--no-overlay', action='store_true', help='Disable overlay')
    parser.add_argument('--test', action='store_true', help='Test memory reading')
    
    args = parser.parse_args()
    
    if args.scan:
        print("[*] Starting offset scanner...")
        pm = pymem.Pymem("GeometryDash.exe")
        scanner = OffsetScanner(pm)
        addr = scanner.find_player_y()
        if addr:
            print(f"[+] Found Y at: 0x{addr:08X}")
        else:
            print("[!] No address found")
        return
    
    if args.test:
        bot = GeometryDashBot(use_overlay=False)
        print(f"Player X: {bot.get_player_x()}")
        print(f"Player Y: {bot.get_player_y()}")
        print(f"Game Speed: {bot.get_game_speed()}")
        print(f"Objects: {len(bot.get_objects())}")
        return
    
    # Run full bot
    bot = GeometryDashBot(use_overlay=not args.no_overlay)
    
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()
        print("\n[*] Bot terminated")

if __name__ == "__main__":
    main()
