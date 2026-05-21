import threading

scan_lock = threading.Lock()
is_scanning = False
