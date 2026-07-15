import requests
import json
import os
import configparser
from io import BytesIO
import zipfile
import shutil
import threading
import queue
import logging
import time
from PIL import Image, ImageTk, PngImagePlugin
import markdown  # For markdown conversion

# Import ttkbootstrap and tkinter modules
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from tkinter import messagebox, filedialog
import re  # Import regular expressions module
import tkinter as tk # For Canvas widget

# Reuse the authenticated "own cards" lookup from the stats server.
import chub_stats_server

# Determine application path for PyInstaller compatibility
import sys
if getattr(sys, 'frozen', False):
    # If the application is run as a bundle, the PyInstaller bootloader
    # extends the sys module by a flag frozen=True and sets the app 
    # path into variable _MEIPASS'. For a one-file bundle, sys.executable is the path to the exe.
    application_path = os.path.dirname(sys.executable)
elif __file__:
    application_path = os.path.dirname(__file__)
else:
    application_path = os.getcwd()

# Configure logging
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# File handler for downloader.log (INFO and above)
info_handler = logging.FileHandler("downloader.log")
info_handler.setLevel(logging.INFO)
info_handler.setFormatter(log_formatter)

# File handler for error.log (ERROR and above)
error_log_file = os.path.join(application_path, 'error.log')
error_handler = logging.FileHandler(error_log_file)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(log_formatter)

# Stream handler for console output (INFO and above)
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(log_formatter)

# Get the root logger and add handlers
logger = logging.getLogger()
logger.setLevel(logging.INFO) # Set the lowest level for the logger itself
logger.addHandler(info_handler)
logger.addHandler(error_handler)
logger.addHandler(stream_handler)

# Define the highlight color
highlight_color = '#859412'

# GUI Setup
app = ttk.Window(
    title="Chub.ai Card Downloader",
    themename="journal"
)
app.geometry("500x300")  # Increased height for status bar

style = ttk.Style()
style.configure('TLabel', font=('Segoe UI', 11))
style.configure('TEntry', font=('Segoe UI', 11))
style.configure('TCombobox', font=('Segoe UI', 11))
style.configure('Custom.TButton', font=('Segoe UI', 11), foreground='white', background=highlight_color)
style.map('Custom.TButton',
          background=[('active', highlight_color)],
          foreground=[('active', 'white')])

# Remove the red border around buttons
style.configure('Custom.TButton', borderwidth=0)
style.configure('TCombobox', fieldbackground='white')

# Initialize config parser
config = configparser.ConfigParser()
config_file = os.path.join(application_path, 'config.ini')

if not os.path.exists(config_file):
    config['Settings'] = {
        'bundle_option': 'Folder',
        'output_directory': '',
        'api_token': ''
    }
    with open(config_file, 'w') as configfile:
        config.write(configfile)
else:
    config.read(config_file)
    # Ensure all settings are present
    if 'bundle_option' not in config['Settings']:
        config['Settings']['bundle_option'] = 'Folder'
    if 'output_directory' not in config['Settings']:
        config['Settings']['output_directory'] = ''
    if 'api_token' not in config['Settings']:
        config['Settings']['api_token'] = ''

def save_config():
    with open(config_file, 'w') as configfile:
        config.write(configfile)

MAX_AI_RATING_API_CALLS = 8 # Max calls: 1 (for initial check) + ceil(log2(100))=7
# Delay between consecutive Chub API calls to avoid rate-limiting / read timeouts.
API_CALL_DELAY = 1.0 # seconds

# Retry policy for HTTP 429 rate-limit responses, mirroring the ForksScanner
# smartFetch approach: up to 5 retries with exponential backoff (5s, 10s, 20s,
# 40s, 80s) before giving up.
RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_BASE_DELAY = 5.0 # seconds

def request_with_retry(url, headers=None, timeout=30, stream=False, status_var=None, context_label=""):
    """GET request that retries on HTTP 429 with exponential backoff.

    Mirrors ForksScanner.html's smartFetch: max 5 retries, delays of 5s, 10s,
    20s, 40s, 80s. Other HTTP/network errors are raised immediately (no retry).
    Returns the requests.Response on success.
    """
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        response = requests.get(url, headers=headers, timeout=timeout, stream=stream)
        if response.status_code != 429:
            return response
        # Rate limited — back off and retry unless we're out of attempts.
        if attempt >= RATE_LIMIT_MAX_RETRIES:
            logging.error(f"HTTP 429 max retries reached for {url}")
            response.raise_for_status()  # raises HTTPError for the 429
            return response  # defensive; raise_for_status above will have raised
        delay = RATE_LIMIT_BASE_DELAY * (2 ** attempt)
        logging.warning(f"Rate limit (429) hit for {url}. Retry {attempt+1}/{RATE_LIMIT_MAX_RETRIES} after {delay}s.")
        if status_var:
            try:
                status_var.set(f"{context_label}Rate limited, retrying in {int(delay)}s (attempt {attempt+1}/{RATE_LIMIT_MAX_RETRIES})...")
            except Exception:
                pass
        time.sleep(delay)
    # Unreachable, but keeps the linter happy.
    return response

def _check_card_rating_api(full_path_query, target_card_id, min_rating_to_check, headers, status_var, current_api_call_num):
    """Helper function to check if a card appears in search with a given min_ai_rating."""
    if status_var:
        status_var.set(f"Checking AI rating... (Call {current_api_call_num}/{MAX_AI_RATING_API_CALLS}, rating >= {min_rating_to_check})")
    
    # Search by target_card_id for AI rating check, include nsfw=true and nsfl=true
    search_url = f"https://api.chub.ai/search?search={target_card_id}&min_ai_rating={min_rating_to_check}&nsfw=true&nsfl=true"
    try:
        response = requests.get(search_url, headers=headers, timeout=15)
        response.raise_for_status()
        initial_response = response.json()
        nodes = initial_response.get('data', {}).get('nodes', [])
        for node_item in nodes:
            if node_item.get('id') == target_card_id:
                return True
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"API error while checking AI rating for {full_path_query} (min_rating {min_rating_to_check}): {e}")
        # Depending on strictness, could raise this or return False/None to indicate failure
        if status_var:
            status_var.set(f"API error checking rating. See logs.")
        return False # Assume not found on error to prevent infinite loops or wrong rating
    finally:
        # Throttle to avoid read timeouts / rate limits during the rating binary search.
        time.sleep(API_CALL_DELAY)

def determine_ai_rating(selected_node_data, headers, status_var=None):
    """Determines the AI rating of a card using binary search on the search API."""
    full_path = selected_node_data.get('fullPath')
    # Fallback for direct API calls that might use 'path' instead of 'fullPath'
    if not full_path and 'path' in selected_node_data:
        full_path = selected_node_data['path']
    target_card_id = selected_node_data.get('id')

    if not full_path or target_card_id is None:
        logging.error("Cannot determine AI rating: fullPath or ID missing from selected_node_data.")
        if status_var: status_var.set("Error: Card data incomplete for AI rating.")
        return 0, 0 # Default to 0 rating, 0 calls if essential info is missing

    num_api_calls = 0
    determined_rating = 0

    # 1. Initial check: Is rating >= 1?
    num_api_calls += 1
    if not _check_card_rating_api(full_path, target_card_id, 1, headers, status_var, num_api_calls):
        # If not found with min_ai_rating=1, then its rating is 0.
        if status_var: status_var.set(f"AI Rating determined: 0 (in {num_api_calls} calls).")
        return 0, num_api_calls
    
    determined_rating = 1 # Known to be at least 1

    # 2. Binary search for rating in [1, 100]
    low = 1
    high = 100
    # determined_rating is already 1, from the check above.
    # The loop will refine it if it's higher.

    while low <= high:
        mid = low + (high - low) // 2
        if mid == 0: # Should not happen if low starts at 1
            low = 1
            continue

        num_api_calls += 1
        if num_api_calls > MAX_AI_RATING_API_CALLS: # Safety break
            logging.warning(f"Exceeded max API calls ({MAX_AI_RATING_API_CALLS}) determining AI rating for {full_path}. Returning current: {determined_rating}")
            break

        if _check_card_rating_api(full_path, target_card_id, mid, headers, status_var, num_api_calls):
            determined_rating = mid # Card's rating is at least 'mid'
            low = mid + 1         # Try for a higher rating
        else:
            high = mid - 1        # Card's rating is less than 'mid'
    
    if status_var: status_var.set(f"AI Rating determined: {determined_rating} (in {num_api_calls} calls).")
    return determined_rating, num_api_calls

def sanitize_filename(name):
    """
    Removes invalid characters from filenames and directory names.
    """
    # Remove invalid characters for Windows filenames
    invalid_chars = r'<>:"/\\|?*'
    sanitized_name = re.sub(f'[{re.escape(invalid_chars)}]', '', name)
    # Remove trailing spaces and periods
    sanitized_name = sanitized_name.rstrip('. ')
    return sanitized_name

def set_api_token():
    """
    Opens a new window to set the Chub.ai Token with an option to toggle visibility.
    """
    def toggle_token_visibility():
        if token_entry.cget('show') == '':
            token_entry.config(show='*')
            eye_button.config(text='👁️')
        else:
            token_entry.config(show='')
            eye_button.config(text='🙈')

    def save_token():
        token = token_entry.get().strip()
        config['Settings']['api_token'] = token
        save_config()
        token_window.destroy()
        messagebox.showinfo("Chub.ai Token Saved", "Your Chub.ai token has been saved.")

    # Create a new window
    token_window = ttk.Toplevel(app)
    token_window.title("Set Chub.ai Token")
    token_window.geometry("400x400")  # Updated size as per your request
    token_window.resizable(False, False)

    # Explanation Label
    explanation = (
        "To access NSFL cards or private content, you need to provide your Chub.ai token.\n\n"
        "How to find your Chub.ai token:\n"
        "1. Open your web browser and go to chub.ai.\n"
        "2. Log in to your account.\n"
        "3. Open the browser's developer tools (usually by pressing F12).\n"
        "4. Go to the 'Application' (or 'Storage') tab.\n"
        "5. Look for 'Local Storage' and find the key 'URQL_TOKEN'.\n"
        "6. Copy the value of 'URQL_TOKEN' and paste it below."
    )
    label = ttk.Label(token_window, text=explanation, wraplength=380, justify=LEFT)
    label.pack(pady=10, padx=10)

    # Token Entry Frame
    token_frame = ttk.Frame(token_window)
    token_frame.pack(pady=(0, 10), padx=10, fill=X)

    # Token Entry Label
    token_label = ttk.Label(token_frame, text="Chub.ai Token:")
    token_label.pack(side=LEFT, pady=(10, 5))

    # Token Entry
    token_entry = ttk.Entry(token_frame, show='*')
    token_entry.insert(0, config['Settings']['api_token'])
    token_entry.pack(side=LEFT, fill=X, expand=YES, pady=(10, 5))

    # Eye Button to Toggle Visibility
    eye_button = ttk.Button(token_frame, text='👁️', width=2, command=toggle_token_visibility)
    eye_button.pack(side=LEFT, padx=(5, 0), pady=(10, 5))

    # Save Button
    save_button = ttk.Button(token_window, text="Save Token", command=save_token, style='Custom.TButton')
    save_button.pack(pady=(0, 10))

class CreatorLookupPopup(tk.Toplevel):
    """Popup to look up a Chub creator's numeric ID by username."""
    def __init__(self, parent, headers):
        super().__init__(parent)
        self.title("Look up Creator ID")
        self.parent = parent
        self.headers = headers
        self.creator_id = None  # Set when the user confirms a found creator

        self.transient(parent)
        self.grab_set()

        main_frame = ttk.Frame(self, padding="15")
        main_frame.pack(fill=BOTH, expand=True)

        ttk.Label(main_frame, text="Enter a creator username to look up their ID:").pack(anchor='w', pady=(0, 5))

        entry_frame = ttk.Frame(main_frame)
        entry_frame.pack(fill=X, pady=2)
        self.creator_name_var = tk.StringVar()
        self.name_entry = ttk.Entry(entry_frame, textvariable=self.creator_name_var, width=40)
        self.name_entry.pack(side=LEFT, fill=X, expand=YES)
        self.name_entry.bind('<Return>', lambda e: self.perform_lookup())
        self.name_entry.focus_set()

        self.lookup_button = ttk.Button(entry_frame, text="Look up", command=self.perform_lookup, style='Custom.TButton')
        self.lookup_button.pack(side=LEFT, padx=(5, 0))

        # Result area (avatar + id/username populated after a lookup)
        self.result_frame = ttk.Frame(main_frame)
        self.result_frame.pack(fill=X, pady=(10, 0))
        self.result_frame.grid_columnconfigure(0, weight=1)

        # Status line
        self.status_var = tk.StringVar()
        self.status_label = ttk.Label(main_frame, textvariable=self.status_var, font=('Segoe UI', 9))
        self.status_label.pack(anchor='w', pady=(5, 0))

        # Action buttons
        action_frame = ttk.Frame(main_frame)
        action_frame.pack(side=BOTTOM, fill=X, pady=(10, 0))
        self.use_button = ttk.Button(action_frame, text="Use this ID", command=self.use_id, state=DISABLED, style='Custom.TButton')
        self.use_button.pack(side=LEFT, padx=5)
        ttk.Button(action_frame, text="Close", command=self.on_close).pack(side=LEFT)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def perform_lookup(self):
        name = self.creator_name_var.get().strip()
        if not name:
            messagebox.showwarning("Input Error", "Please enter a creator username.", parent=self)
            return

        self.lookup_button.config(state=DISABLED)
        self.use_button.config(state=DISABLED)
        self.status_var.set(f"Looking up '{name}'...")
        threading.Thread(target=self._do_lookup, args=(name,), daemon=True).start()

    def _do_lookup(self, name):
        try:
            url = f"https://api.chub.ai/api/users/{name}?nsfl=true&exclude_mine=false&include_projects=false"
            response = requests.get(url, headers=self.headers, timeout=15)

            if response.status_code == 404:
                self.parent.after(0, lambda: self._show_not_found(name))
                return
            response.raise_for_status()
            data = response.json()

            if not data or 'id' not in data or data.get('error'):
                self.parent.after(0, lambda: self._show_not_found(name))
                return

            self.parent.after(0, lambda: self._show_result(data))
        except requests.exceptions.RequestException as e:
            self.parent.after(0, lambda: self._show_error(str(e)))
        except Exception as e:
            self.parent.after(0, lambda: self._show_error(str(e)))

    def _clear_result(self):
        for widget in self.result_frame.winfo_children():
            widget.destroy()

    def _show_result(self, data):
        self._clear_result()

        creator_id = data.get('id')
        username = data.get('username', 'N/A')
        display_name = data.get('name', '') or ''
        avatar_url = data.get('avatar_url')

        self.creator_id = str(creator_id)

        info_text = f"ID: {creator_id}    Username: {username}"
        if display_name:
            info_text += f"    Name: {display_name}"
        ttk.Label(self.result_frame, text=info_text, font=('Segoe UI', 10, 'bold')).grid(row=0, column=0, sticky='w')

        if avatar_url:
            img_label = ttk.Label(self.result_frame)
            img_label.grid(row=1, column=0, sticky='w', pady=(5, 0))
            threading.Thread(target=self._load_avatar, args=(avatar_url, img_label), daemon=True).start()

        self.status_var.set("Creator found. Click 'Use this ID' to fill in the field.")
        self.use_button.config(state=NORMAL)
        self.lookup_button.config(state=NORMAL)

    def _show_not_found(self, name):
        self._clear_result()
        self.creator_id = None
        self.status_var.set(f"No creator found with username '{name}'.")
        self.lookup_button.config(state=NORMAL)
        self.use_button.config(state=DISABLED)

    def _show_error(self, error_msg):
        self._clear_result()
        self.creator_id = None
        self.status_var.set(f"Error: {error_msg}")
        self.lookup_button.config(state=NORMAL)
        self.use_button.config(state=DISABLED)

    def _load_avatar(self, url, img_label):
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, stream=True, timeout=10)
            response.raise_for_status()
            image = Image.open(BytesIO(response.content))
            image.thumbnail((80, 80))
            photo = ImageTk.PhotoImage(image)
            img_label.image = photo  # prevent garbage collection
            self.parent.after(0, lambda: img_label.config(image=photo, text=""))
        except Exception as e:
            logging.error(f"Error loading avatar from {url}: {e}")
            self.parent.after(0, lambda: img_label.config(text="Avatar N/A"))

    def use_id(self):
        # Keep self.creator_id set; parent reads it after the window closes.
        self.grab_release()
        self.destroy()

    def on_close(self):
        self.creator_id = None
        self.grab_release()
        self.destroy()


class AdvancedSearchPopup(tk.Toplevel):
    def __init__(self, parent, headers):
        super().__init__(parent)
        self.title("Advanced Search")
        self.parent = parent
        self.headers = headers
        self.selected_card_node = None

        # Load filters from config
        if not config.has_section('AdvancedSearch'):
            config.add_section('AdvancedSearch')
        
        self.search_query_var = tk.StringVar(value=entry.get()) # Carry over search query
        self.sort_by_var = tk.StringVar(value=config.get('AdvancedSearch', 'sort_by', fallback='download_count'))
        self.sort_asc_var = tk.BooleanVar(value=config.getboolean('AdvancedSearch', 'sort_asc', fallback=False))
        self.nsfw_var = tk.BooleanVar(value=config.getboolean('AdvancedSearch', 'nsfw', fallback=True))
        self.nsfl_var = tk.BooleanVar(value=config.getboolean('AdvancedSearch', 'nsfl', fallback=False))
        self.min_tokens_var = tk.StringVar(value=config.get('AdvancedSearch', 'min_tokens', fallback=''))
        self.max_tokens_var = tk.StringVar(value=config.get('AdvancedSearch', 'max_tokens', fallback=''))
        self.max_days_ago_var = tk.StringVar(value=config.get('AdvancedSearch', 'max_days_ago', fallback=''))
        self.creator_id_var = tk.StringVar(value=config.get('AdvancedSearch', 'creator_id', fallback=''))
        self.topics_var = tk.StringVar(value=config.get('AdvancedSearch', 'topics', fallback=''))
        self.exclude_topics_var = tk.StringVar(value=config.get('AdvancedSearch', 'exclude_topics', fallback=''))

        self.transient(parent)
        self.grab_set()
        self.create_widgets()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def create_widgets(self):
        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill=BOTH, expand=True)

        # Search Query
        ttk.Label(main_frame, text="Search Query:").grid(row=0, column=0, sticky='w', pady=2)
        ttk.Entry(main_frame, textvariable=self.search_query_var, width=80).grid(row=0, column=1, columnspan=3, sticky='we', pady=2)

        # Sorting Frame
        sort_frame = ttk.LabelFrame(main_frame, text="Sorting", padding="10")
        sort_frame.grid(row=1, column=0, columnspan=4, sticky='we', pady=5)
        sort_frame.grid_columnconfigure(1, weight=1)

        ttk.Label(sort_frame, text="Sort by:").grid(row=0, column=0, sticky='w')
        sort_options = ['download_count', 'last_activity_at', 'user_count', 'rating_count', 'rating_avg', 'created_at', 'token_count', 'msgs_chat', 'msgs_total', 'name', 'full_path']
        ttk.Combobox(sort_frame, textvariable=self.sort_by_var, values=sort_options, state='readonly').grid(row=0, column=1, sticky='we', padx=5)
        
        order_frame = ttk.Frame(sort_frame)
        order_frame.grid(row=0, column=2, columnspan=2)
        ttk.Radiobutton(order_frame, text="Asc", variable=self.sort_asc_var, value=True).pack(side=LEFT, padx=5)
        ttk.Radiobutton(order_frame, text="Desc", variable=self.sort_asc_var, value=False).pack(side=LEFT, padx=5)

        # Filters Frame
        filters_frame = ttk.LabelFrame(main_frame, text="Filters", padding="10")
        filters_frame.grid(row=2, column=0, columnspan=4, sticky='we', pady=5)
        filters_frame.grid_columnconfigure(1, weight=1)
        filters_frame.grid_columnconfigure(3, weight=1)

        ttk.Checkbutton(filters_frame, text="Include NSFW", variable=self.nsfw_var).grid(row=0, column=0, sticky='w')
        ttk.Checkbutton(filters_frame, text="Include NSFL", variable=self.nsfl_var).grid(row=0, column=1, sticky='w')

        ttk.Label(filters_frame, text="Min Tokens:").grid(row=1, column=0, sticky='w', pady=2, padx=5)
        ttk.Entry(filters_frame, textvariable=self.min_tokens_var).grid(row=1, column=1, sticky='we', pady=2, padx=5)
        ttk.Label(filters_frame, text="Max Tokens:").grid(row=1, column=2, sticky='w', pady=2, padx=5)
        ttk.Entry(filters_frame, textvariable=self.max_tokens_var).grid(row=1, column=3, sticky='we', pady=2, padx=5)

        ttk.Label(filters_frame, text="Max Days Ago:").grid(row=2, column=0, sticky='w', pady=2, padx=5)
        ttk.Entry(filters_frame, textvariable=self.max_days_ago_var).grid(row=2, column=1, sticky='we', pady=2, padx=5)
        ttk.Label(filters_frame, text="Creator ID:").grid(row=2, column=2, sticky='w', pady=2, padx=5)
        creator_id_frame = ttk.Frame(filters_frame)
        creator_id_frame.grid(row=2, column=3, sticky='we', pady=2, padx=5)
        creator_id_frame.grid_columnconfigure(0, weight=1)
        ttk.Entry(creator_id_frame, textvariable=self.creator_id_var).grid(row=0, column=0, sticky='we')
        ttk.Button(creator_id_frame, text="🔍", width=3, command=self.open_creator_lookup).grid(row=0, column=1, padx=(2, 0))

        ttk.Label(filters_frame, text="Tags (csv):").grid(row=3, column=0, sticky='w', pady=2, padx=5)
        ttk.Entry(filters_frame, textvariable=self.topics_var).grid(row=3, column=1, sticky='we', pady=2, padx=5)
        ttk.Label(filters_frame, text="Exclude Tags (csv):").grid(row=3, column=2, sticky='w', pady=2, padx=5)
        ttk.Entry(filters_frame, textvariable=self.exclude_topics_var).grid(row=3, column=3, sticky='we', pady=2, padx=5)

        # Presets Frame
        presets_frame = ttk.LabelFrame(main_frame, text="Presets", padding="10")
        presets_frame.grid(row=3, column=0, columnspan=4, sticky='we', pady=10)
        presets_frame.grid_columnconfigure(0, weight=1)
        presets_frame.grid_columnconfigure(1, weight=1)
        presets_frame.grid_columnconfigure(2, weight=1)

        ttk.Button(presets_frame, text="Latest", command=self.set_latest_preset, style='Custom.TButton').grid(row=0, column=0, sticky='ew', padx=5)
        ttk.Button(presets_frame, text="Trending", command=self.set_trending_preset, style='Custom.TButton').grid(row=0, column=1, sticky='ew', padx=5)
        ttk.Button(presets_frame, text="Recent Hits", command=self.set_recent_hits_preset, style='Custom.TButton').grid(row=0, column=2, sticky='ew', padx=5)

        # Action Buttons
        action_frame = ttk.Frame(main_frame)
        action_frame.grid(row=4, column=0, columnspan=4, sticky='e', pady=(5,0))
        self.search_button = ttk.Button(action_frame, text="Search", command=self.start_advanced_search, style='Custom.TButton')
        self.search_button.pack(side=LEFT, padx=5)
        ttk.Button(action_frame, text="Cancel", command=self.on_close).pack(side=LEFT)

    def open_creator_lookup(self):
        popup = CreatorLookupPopup(self, self.headers)
        self.wait_window(popup)
        if popup.creator_id:
            self.creator_id_var.set(popup.creator_id)

    def save_filters_to_config(self):
        if not config.has_section('AdvancedSearch'):
            config.add_section('AdvancedSearch')
        config.set('AdvancedSearch', 'sort_by', self.sort_by_var.get())
        config.set('AdvancedSearch', 'sort_asc', str(self.sort_asc_var.get()))
        config.set('AdvancedSearch', 'nsfw', str(self.nsfw_var.get()))
        config.set('AdvancedSearch', 'nsfl', str(self.nsfl_var.get()))
        config.set('AdvancedSearch', 'min_tokens', self.min_tokens_var.get())
        config.set('AdvancedSearch', 'max_tokens', self.max_tokens_var.get())
        config.set('AdvancedSearch', 'max_days_ago', self.max_days_ago_var.get())
        config.set('AdvancedSearch', 'creator_id', self.creator_id_var.get())
        config.set('AdvancedSearch', 'topics', self.topics_var.get())
        config.set('AdvancedSearch', 'exclude_topics', self.exclude_topics_var.get())
        save_config()

    def set_latest_preset(self):
        self.sort_by_var.set('created_at')
        self.sort_asc_var.set(False)
        self.max_days_ago_var.set('')
        self.min_tokens_var.set('')
        self.max_tokens_var.set('')
        self.creator_id_var.set('')
        self.topics_var.set('')
        self.exclude_topics_var.set('')

    def set_trending_preset(self):
        self.sort_by_var.set('trending')
        self.sort_asc_var.set(False)
        self.max_days_ago_var.set('7')
        self.min_tokens_var.set('')
        self.max_tokens_var.set('')
        self.creator_id_var.set('')
        self.topics_var.set('')
        self.exclude_topics_var.set('')

    def set_recent_hits_preset(self):
        self.sort_by_var.set('trending')
        self.sort_asc_var.set(False)
        self.max_days_ago_var.set('1')
        self.min_tokens_var.set('')
        self.max_tokens_var.set('')
        self.creator_id_var.set('')
        self.topics_var.set('')
        self.exclude_topics_var.set('')

    def start_advanced_search(self):
        self.save_filters_to_config()
        self.search_button.config(state=DISABLED)
        status_var.set("Executing advanced search...")
        threading.Thread(target=self.execute_advanced_search, daemon=True).start()

    def execute_advanced_search(self):
        try:
            base_url = "https://api.chub.ai/search"
            params = {
                'search': self.search_query_var.get(),
                'sort': self.sort_by_var.get(),
                'sort_dir': 'asc' if self.sort_asc_var.get() else 'desc',
                'nsfw': self.nsfw_var.get(),
                'nsfl': self.nsfl_var.get(),
                'min_tokens': self.min_tokens_var.get(),
                'max_tokens': self.max_tokens_var.get(),
                'max_days_ago': self.max_days_ago_var.get(),
                'creator_id': self.creator_id_var.get(),
                'topics': self.topics_var.get(),
                'exclude_topics': self.exclude_topics_var.get(),
                'first': 20, # Corresponds to results_per_page in CardSelectionPopup
                'page': 1
            }
            
            # Clean up empty parameters
            search_params = {k: v for k, v in params.items() if v not in [None, '', False]}
            if self.nsfw_var.get(): search_params['nsfw'] = 'true'
            if self.nsfl_var.get(): search_params['nsfl'] = 'true'

            # The Chub API returns 0 results when combining sort=trending with a
            # creator_id filter (server-side limitation). Fall back to a sort
            # that supports creator filtering so the user still gets results.
            if search_params.get('creator_id') and search_params.get('sort') == 'trending':
                fallback_sort = 'download_count'
                logging.info("sort=trending is incompatible with creator_id; falling back to %s.", fallback_sort)
                search_params['sort'] = fallback_sort

            response = requests.get(base_url, headers=self.headers, params=search_params)
            response.raise_for_status()
            api_response_data = response.json()

            nodes = api_response_data.get('data', {}).get('nodes', [])
            count = api_response_data.get('data', {}).get('count', 0)

            if count == 0:
                self.parent.after(0, lambda: messagebox.showinfo("No Results", "No cards found with the specified criteria."))
                self.parent.after(0, self.on_close)
                return
            elif count == 1:
                # Single result: download directly, no selection popup needed.
                self.parent.after(0, lambda: self.start_download(nodes[0]))
                self.parent.after(0, self.on_close)
            else:
                # Multiple results: open the selection popup with a download
                # callback so the user can download multiple cards from the
                # same search without the window closing.
                self.parent.after(0, lambda: self.show_selection_popup(api_response_data, search_params))

        except requests.exceptions.HTTPError as http_err:
            try:
                error_detail = http_err.response.json()
                self.parent.after(0, lambda: messagebox.showerror("HTTP Error", f"A server error occurred: {http_err.response.status_code}\nDetails: {error_detail}"))
            except json.JSONDecodeError:
                self.parent.after(0, lambda: messagebox.showerror("HTTP Error", f"An HTTP error occurred: {http_err}"))
            self.parent.after(0, self.on_close)
        except Exception as err:
            logging.error(f"An error occurred during advanced search: {err}")
            self.parent.after(0, lambda: messagebox.showerror("Error", f"An unexpected error occurred: {err}"))
            self.parent.after(0, self.on_close)

    def show_selection_popup(self, api_response, search_params):
        # Opens the selection popup with a download callback. The popup stays
        # open after each download so the user can pick multiple cards.
        # search_params is reused for pagination so filters stay consistent
        # across pages. When the popup closes, the Search button is re-enabled
        # so a new search can be run without reopening this window.
        def on_results_closed():
            try:
                self.search_button.config(state=NORMAL)
            except Exception:
                pass

        popup = CardSelectionPopup(
            self.parent,
            self.search_query_var.get(),
            api_response,
            self.headers,
            download_callback=self.start_download,
            search_params=search_params,
            on_close_callback=on_results_closed,
        )

    def start_download(self, node):
        bundle_option = var.get()
        output_directory = output_dir.get()
        status_var.set(f"Card selected: {node.get('name', 'Unknown')}. Starting download...")
        # Disable buttons in the main app window
        set_ui_state(DISABLED)
        threading.Thread(target=download_card_thread, args=(node, bundle_option, output_directory, self.headers, None, status_var, set_ui_state), daemon=True).start()

    def on_close(self):
        self.grab_release()
        self.destroy()

    def show(self):
        # This method is kept for compatibility but the main logic is now in show_selection_popup
        self.wait_window(self)
        return self.selected_card_node

class CardSelectionPopup(ttk.Toplevel):
    def __init__(self, parent, query, initial_response, headers, download_callback=None, search_params=None, on_close_callback=None):
        super().__init__(parent)
        self.title("Select a Card")
        self.geometry("700x650")
        self.parent = parent
        self.query = query
        self.headers = headers
        self.selected_card_node = None
        # When set, clicking "Download this Card" invokes this callback with the
        # node and keeps the popup open, allowing multiple downloads per search.
        self.download_callback = download_callback
        # Full filter dict used for the original search; reused for pagination so
        # filters (creator_id, sort, topics, ...) stay consistent across pages.
        self.search_params = dict(search_params) if search_params else {
            'search': query, 'nsfw': 'true', 'nsfl': 'true'
        }
        # Optional callback invoked after this popup closes (e.g. to re-enable
        # the parent's Search button so a new search can be run).
        self.on_close_callback = on_close_callback

        self.page_cache = {}
        self.image_cache = {}
        self.current_page = 1
        self.results_per_page = 20  # Corresponds to MAX_CARDS_TO_DISPLAY
        self.total_results = initial_response.get('data', {}).get('count', 0)
        self.total_pages = (self.total_results + self.results_per_page - 1) // self.results_per_page

        # Cache the first page
        self.page_cache[1] = initial_response.get('data', {}).get('nodes', [])

        self.transient(parent)
        self.grab_set()

        self.create_widgets()
        self.populate_page(self.current_page)
        self.prefetch_next_page()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def create_widgets(self):
        # Main container
        main_frame = ttk.Frame(self, padding=10)
        main_frame.pack(fill=BOTH, expand=YES)

        # Canvas for scrolling content
        canvas = tk.Canvas(main_frame, borderwidth=0, background="#ffffff")
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
        self.scrollable_frame = ttk.Frame(canvas)

        self.scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        # Pagination controls
        pagination_frame = ttk.Frame(self, padding=(10, 5))
        pagination_frame.pack(fill=X, side=BOTTOM)

        self.prev_button = ttk.Button(pagination_frame, text="<< Previous", command=self.prev_page, style='Custom.TButton')
        self.prev_button.pack(side=LEFT, padx=5)

        self.page_label = ttk.Label(pagination_frame, text=f"Page {self.current_page} / {self.total_pages}")
        self.page_label.pack(side=LEFT, expand=True)

        self.next_button = ttk.Button(pagination_frame, text="Next >>", command=self.next_page, style='Custom.TButton')
        self.next_button.pack(side=RIGHT, padx=5)

    def populate_page(self, page_number):
        # Clear existing widgets
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        cards_data = self.page_cache.get(page_number, [])
        if not cards_data:
            ttk.Label(self.scrollable_frame, text="No cards to display.").pack(padx=10, pady=10)
            return

        for card_node in cards_data:
            self.create_card_widget(card_node)

        self.update_pagination_controls()
        self.prefetch_next_page()

    def create_card_widget(self, card_node):
        card_item_frame = ttk.Frame(self.scrollable_frame, padding=10, relief=SOLID, borderwidth=1)
        card_item_frame.pack(pady=10, padx=10, fill=X, expand=YES)

        left_frame = ttk.Frame(card_item_frame)
        left_frame.pack(side=LEFT, padx=(0, 10), fill=Y)

        avatar_url = card_node.get('avatar_url')
        img_label = ttk.Label(left_frame, text="Loading...")
        img_label.pack(pady=5, padx=5)

        if avatar_url:
            if avatar_url in self.image_cache:
                img_label.config(image=self.image_cache[avatar_url], text="")
            else:
                threading.Thread(target=self.load_image, args=(avatar_url, img_label), daemon=True).start()
        else:
            img_label.config(text="No Avatar")

        right_frame = ttk.Frame(card_item_frame)
        right_frame.pack(side=LEFT, fill=X, expand=YES)

        name_label = ttk.Label(right_frame, text=f"{card_node.get('name', 'N/A')}", font=('Segoe UI', 12, 'bold'), wraplength=450)
        name_label.pack(anchor=W, pady=(0, 2))

        path_label = ttk.Label(right_frame, text=f"Path: {card_node.get('fullPath', 'N/A')}", font=('Segoe UI', 9), wraplength=450)
        path_label.pack(anchor=W)

        tagline_text = card_node.get('tagline', 'N/A') or "No tagline available."
        tagline_label = ttk.Label(right_frame, text=tagline_text, font=('Segoe UI', 10), wraplength=450, justify=LEFT)
        tagline_label.pack(anchor=W, pady=(5, 10), fill=X, expand=YES)

        select_button = ttk.Button(right_frame, text="Download this Card", command=lambda cn=card_node: self.on_select(cn), style='Custom.TButton')
        select_button.pack(anchor=E, pady=5)

    def update_pagination_controls(self):
        self.page_label.config(text=f"Page {self.current_page} / {self.total_pages}")
        self.prev_button.config(state=NORMAL if self.current_page > 1 else DISABLED)
        self.next_button.config(state=NORMAL if self.current_page < self.total_pages else DISABLED)

    def go_to_page(self, page_number):
        if not (1 <= page_number <= self.total_pages):
            return

        self.current_page = page_number
        if page_number in self.page_cache:
            self.populate_page(page_number)
        else:
            ttk.Label(self.scrollable_frame, text="Loading page...").pack(pady=20)
            threading.Thread(target=self.fetch_and_display_page, args=(page_number,), daemon=True).start()

    def fetch_and_display_page(self, page_number):
        try:
            params = dict(self.search_params)
            params['page'] = page_number
            params['first'] = self.results_per_page
            response = requests.get("https://api.chub.ai/search", headers=self.headers, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            nodes = data.get('data', {}).get('nodes', [])
            self.page_cache[page_number] = nodes
            self.parent.after(0, self.populate_page, page_number)
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to fetch page {page_number}: {e}")
            self.parent.after(0, lambda: messagebox.showerror("API Error", f"Failed to fetch page {page_number}."))

    def prefetch_next_page(self):
        next_page = self.current_page + 1
        if 1 <= next_page <= self.total_pages and next_page not in self.page_cache:
            threading.Thread(target=self.fetch_page_data, args=(next_page,), daemon=True).start()

    def fetch_page_data(self, page_number):
        if page_number in self.page_cache: # Double check before fetching
            return
        try:
            params = dict(self.search_params)
            params['page'] = page_number
            params['first'] = self.results_per_page
            response = requests.get("https://api.chub.ai/search", headers=self.headers, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            nodes = data.get('data', {}).get('nodes', [])
            self.page_cache[page_number] = nodes

            # Pre-load images for the fetched page
            for card_node in nodes:
                avatar_url = card_node.get('avatar_url')
                if avatar_url and avatar_url not in self.image_cache:
                    threading.Thread(target=self.preload_image_data, args=(avatar_url,), daemon=True).start()
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to prefetch page {page_number}: {e}")

    def next_page(self):
        self.go_to_page(self.current_page + 1)

    def prev_page(self):
        self.go_to_page(self.current_page - 1)

    def on_select(self, card_node):
        if self.download_callback:
            # Multi-download mode: keep the popup open so the user can pick more.
            self.download_callback(card_node)
            return
        self.selected_card_node = card_node
        self.on_close()

    def on_close(self):
        self.selected_card_node = None # Ensure nothing is returned if closed
        self.grab_release()
        self.destroy()
        if self.on_close_callback:
            try:
                self.on_close_callback()
            except Exception as e:
                logging.error(f"CardSelectionPopup on_close_callback failed: {e}")

    def show(self):
        self.wait_window(self)
        return self.selected_card_node

    def preload_image_data(self, url):
        if url in self.image_cache:
            return
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, stream=True, timeout=10)
            response.raise_for_status()
            img_data = response.content
            image = Image.open(BytesIO(img_data))
            image.thumbnail((100, 100))
            photo = ImageTk.PhotoImage(image)
            self.image_cache[url] = photo
        except Exception as e:
            logging.error(f"Error pre-loading image from {url}: {e}")

    def load_image(self, url, img_label):
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, stream=True, timeout=10)
            response.raise_for_status()
            img_data = response.content
            image = Image.open(BytesIO(img_data))
            image.thumbnail((100, 100))
            photo = ImageTk.PhotoImage(image)
            self.image_cache[url] = photo
            self.parent.after(0, lambda: img_label.config(image=photo, text=""))
        except Exception as e:
            logging.error(f"Error loading image from {url}: {e}")
            self.parent.after(0, lambda: img_label.config(text="Image N/A"))

    def on_close(self):
        self.grab_release()
        self.destroy()

def on_search_click():
    set_ui_state(DISABLED)
    threading.Thread(target=search_and_select_card, daemon=True).start()

def open_advanced_search():
    api_token = config['Settings'].get('api_token', '').strip()
    headers = {
        'accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    if api_token:
        headers['Authorization'] = f'Bearer {api_token}'
    
    adv_popup = AdvancedSearchPopup(app, headers)
    # No need to call show() here as the popup manages its own lifecycle

def on_download_click():
    set_ui_state(DISABLED)
    threading.Thread(target=download_card_direct, daemon=True).start()

def download_card_direct():
    try:
        # Disable buttons during download
        set_ui_state(DISABLED)
        token_button.config(state=DISABLED)
        select_output_button.config(state=DISABLED)
        status_var.set("Downloading card...")

        input_text = entry.get().strip()
        bundle_option = var.get()
        output_directory = output_dir.get()
        api_token = config['Settings'].get('api_token', '').strip()

        if not input_text:
            messagebox.showwarning("Input Error", "Please enter a valid Chub.ai URL or character path.")
            return

        if not output_directory:
            messagebox.showwarning("Output Directory Not Set", "Please select an output directory.")
            return

        # Prepare headers
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        if api_token:
            headers['Authorization'] = f'Bearer {api_token}'

        # Extract character path from input
        character_path = input_text
        if 'characters/' in character_path:
            character_path = character_path.split('characters/')[-1]

        if not character_path:
            messagebox.showwarning("Input Error", "Please enter a valid Chub.ai URL or character path.\nExample: 'https://chub.ai/characters/dabozo/francis-franny-maywood-f8a5a1e15457' or 'dabozo/francis-franny-maywood-f8a5a1e15457'")
            return

        status_var.set(f"Fetching card: {character_path}...")

        # API call to get card data
        api_url = f"https://api.chub.ai/api/characters/{character_path}?full=false"
        response = requests.get(api_url, headers=headers, timeout=15)
        if response.status_code != 200:
            logging.error(f"API Error for {api_url}: Status {response.status_code}, Response: {response.text}")
            messagebox.showerror("API Error", f"Failed to fetch card data (Status: {response.status_code}). Please check the URL/path and logs for details.\n\nValid URL format: https://chub.ai/characters/Sambolic/valerie-you-ex-out-of-prison-6f6f8b916b36")
            return
        card_data = response.json()

        if not card_data or 'node' not in card_data:
            messagebox.showerror("API Error", "Failed to fetch card data. Please check the URL/path.")
            return

        node = card_data.get('node')
        if not node:
            messagebox.showerror("API Error", "No card found with the provided path.")
            return

        # Proceed with download using existing logic
        status_var.set(f"Card found: {node.get('name', 'Unknown')}. Proceeding...")

        # Determine AI Rating
        status_var.set(f"Preparing to determine AI rating for {node.get('name', 'Unknown')}...")
        ai_rating, calls_made = determine_ai_rating(node, headers, status_var)

        # Proceed with the rest of the download process
        download_card_thread(node, bundle_option, output_directory, headers, ai_rating, status_var)

    except requests.exceptions.RequestException as e:
        status_var.set(f"API Error: {e}")
        messagebox.showerror("API Error", f"Failed to connect to Chub.ai API: {e}")
    except Exception as e:
        status_var.set(f"Error: {e}")
        messagebox.showerror("Error", f"An unexpected error occurred: {e}")
    finally:
        # Always re-enable UI
        set_ui_state(NORMAL)
        token_button.config(state=NORMAL)
        select_output_button.config(state=NORMAL)

def download_card_image(node, card_dir, sanitized_name, headers, status_var, batch_prefix="", suppress_popup=False):
    """Download the card's main image, with fallbacks for broken Chub CDN URLs.

    Chub's API sometimes returns a stale max_res_url with a typo in the
    filename ('chara_char_v2.png' instead of 'chara_card_v2.png'). When the
    primary URL 404s we try that substitution, then fall back to avatar_url.
    Returns True on success, False if all attempts failed.
    """
    candidates = []
    max_res_url = node.get('max_res_url')
    avatar_url = node.get('avatar_url')
    if max_res_url:
        candidates.append(max_res_url)
        # Known Chub CDN typo/stale URL: chara_char_v2 -> chara_card_v2
        if 'chara_char_v2' in max_res_url:
            candidates.append(max_res_url.replace('chara_char_v2', 'chara_card_v2'))
    if avatar_url and avatar_url not in candidates:
        candidates.append(avatar_url)

    if not candidates:
        msg = "Could not find 'max_res_url' or 'avatar_url' for the selected card to download the image."
        logging.error(msg + f" Card: {node.get('fullPath') or node.get('path')}")
        if not suppress_popup:
            messagebox.showerror("Download Error", msg)
        return False

    last_error = None
    for url in candidates:
        status_var.set(f"{batch_prefix}Downloading card image from {url[:50]}...")
        try:
            image_response = request_with_retry(url, headers=headers, timeout=30, stream=True, status_var=status_var, context_label=batch_prefix)
            if image_response.status_code == 404:
                logging.warning(f"Card image 404 at {url}; trying next candidate...")
                last_error = f"404 Not Found for url: {url}"
                continue
            image_response.raise_for_status()
            # Preserve the remote extension when falling back to avatar.webp etc.
            ext = os.path.splitext(url.split('?')[0])[1] or '.png'
            out_path = os.path.join(card_dir, f"{sanitized_name}{ext}")
            with open(out_path, 'wb') as img_file:
                for chunk in image_response.iter_content(chunk_size=8192):
                    img_file.write(chunk)
            if url != max_res_url:
                logging.info(f"Downloaded card image via fallback URL: {url}")
            status_var.set(f"{batch_prefix}Card image downloaded.")
            return True
        except requests.exceptions.RequestException as img_err:
            logging.warning(f"Failed to download card image from {url}: {img_err}")
            last_error = str(img_err)
            continue

    logging.error(f"All card image candidates failed for {node.get('fullPath') or node.get('name')}: {last_error}")
    if not suppress_popup:
        messagebox.showerror("Image Download Error", f"Failed to download card image: {last_error}")
    return False


def download_card_thread(node, bundle_option, output_directory, headers, ai_rating, status_var, ui_callback=None, suppress_popup=False, batch_prefix=""):
    try:
        # Check if a card was actually selected/found before proceeding
        if not node:
            status_var.set("No card available for download. Ready.")
            return

        node['ai_rating_determined'] = ai_rating # Store it in the node data for HTML generation

        card_id = node['id']
        full_path = node.get('fullPath') or node.get('path')
        name = node['name']

        # Sanitize the name for use in file paths
        sanitized_name = sanitize_filename(name)

        # Create output directory
        output_dir_path = output_directory
        if not os.path.exists(output_dir_path):
            os.makedirs(output_dir_path)

        card_dir = os.path.join(output_dir_path, sanitized_name)
        if not os.path.exists(card_dir):
            os.makedirs(card_dir)

        # Download PNG using max_res_url (with CDN typo / avatar fallbacks)
        download_card_image(node, card_dir, sanitized_name, headers, status_var, batch_prefix=batch_prefix, suppress_popup=suppress_popup)

        # Third API call to get gallery images
        logging.info(f"Attempting to fetch gallery for card_id: {card_id}")
        gallery_url = f"https://api.chub.ai/api/gallery/project/{card_id}?nsfw=true&page=1&limit=24"
        logging.info(f"Fetching gallery from: {gallery_url}")
        gallery_images = []  # Filenames of successfully downloaded gallery images
        try:
            response = request_with_retry(gallery_url, headers=headers, timeout=15, status_var=status_var, context_label=batch_prefix)
            logging.info(f"Gallery API response status: {response.status_code}")
            response.raise_for_status()
            gallery_data = response.json()
            logging.info(f"Gallery data received: {json.dumps(gallery_data, indent=2)}")

            nodes = gallery_data.get('nodes', [])
            gallery_count = len(nodes) # Use the actual length of the nodes list, not the 'count' field

            if gallery_count > 0:
                status_var.set(f"{batch_prefix}Downloading {gallery_count} gallery images...")
                for i, image_node in enumerate(nodes):
                    # The correct key for the gallery image URL is 'primary_image_path'
                    image_url = image_node.get('primary_image_path')
                    if not image_url:
                        logging.warning(f"No 'primary_image_path' key found for gallery image node {i}: {image_node}")
                        continue

                    logging.info(f"Attempting to download gallery image from URL: {image_url}")
                    status_var.set(f"{batch_prefix}Downloading gallery image {i+1}/{len(nodes)}...")
                    try:
                        image_response = request_with_retry(image_url, timeout=30, status_var=status_var, context_label=batch_prefix)
                        image_response.raise_for_status()
                        image_name = image_url.split('/')[-1].split('?')[0] # Clean query params
                        sanitized_image_name = sanitize_filename(image_name)
                        file_path = os.path.join(card_dir, sanitized_image_name)
                        with open(file_path, 'wb') as img_file:
                            img_file.write(image_response.content)
                        gallery_images.append(sanitized_image_name)
                        logging.info(f"Successfully downloaded and saved gallery image to {file_path}")
                    except requests.exceptions.RequestException as img_err:
                        logging.error(f"Failed to download gallery image {image_url}: {img_err}")
            else:
                logging.info("No gallery images found for this card.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to fetch gallery images: {e}")

        # Generate the HTML after gallery download so it can embed an interactive
        # gallery referencing the downloaded image files.
        html_content = generate_html(node, gallery_images=gallery_images)
        with open(os.path.join(card_dir, f"{sanitized_name}_info.html"), 'w', encoding='utf-8') as f:
            f.write(html_content)

        # Bundle option
        if bundle_option == 'Zip':
            zipf = zipfile.ZipFile(f"{card_dir}.zip", 'w', zipfile.ZIP_DEFLATED)
            for root, dirs, files in os.walk(card_dir):
                for file in files:
                    zipf.write(os.path.join(root, file), arcname=file)
            zipf.close()
            shutil.rmtree(card_dir)
            if not suppress_popup:
                messagebox.showinfo("Success", f"All files have been saved and zipped at {card_dir}.zip")
            status_var.set("Download complete. Ready.")
        else:
            if not suppress_popup:
                messagebox.showinfo("Success", f"All files have been saved in {card_dir}")
            status_var.set("Download complete. Ready.")

    except Exception as err:
        logging.error(f"An error occurred during download: {err}")
        messagebox.showerror("Error", f"An error occurred during download: {err}")
        status_var.set("Error occurred. Ready for new attempt.")
    finally:
        if ui_callback:
            # Use app.after to ensure UI updates are done in the main thread
            app.after(0, ui_callback, NORMAL)

def search_and_select_card():
    app.after(0, set_ui_state, DISABLED)
    try:
        status_var.set("Searching for card...")

        name = entry.get().strip()
        bundle_option = var.get()
        output_directory = output_dir.get()
        api_token = config['Settings'].get('api_token', '').strip()

        if not name:
            messagebox.showwarning("Input Error", "Please enter the name of the card.")
            return

        if not output_directory:
            messagebox.showwarning("Output Directory Not Set", "Please select an output directory.")
            return

        # Headers for API requests
        headers = {
            'accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        if api_token:
            headers['Authorization'] = f'Bearer {api_token}'

        search_url = f"https://api.chub.ai/search?search={name}&page=1&first=20&nsfw=true&nsfl=true&count=true"
        status_var.set(f"Searching for: {name}...")

        response = requests.get(search_url, headers=headers, timeout=15)
        response.raise_for_status()
        initial_response = response.json()

        if not initial_response or 'data' not in initial_response:
            status_var.set("API Error: Invalid response from server.")
            messagebox.showerror("API Error", "Received an invalid or empty response from the server.")
            return

        nodes = initial_response.get('data', {}).get('nodes', [])
        count = initial_response.get('data', {}).get('count', 0)

        if count == 0:
            messagebox.showinfo("No Results", "No card found with the given name.")
            status_var.set("No results found. Ready.")
            return

        def download_selected(node):
            status_var.set(f"Card selected: {node.get('name', 'Unknown')}. Proceeding...")
            status_var.set(f"Preparing to determine AI rating for {node.get('name', 'Unknown')}...")
            ai_rating, calls_made = determine_ai_rating(node, headers, status_var)
            download_card_thread(node, bundle_option, output_directory, headers, ai_rating, status_var)

        if count == 1:
            node = nodes[0]
            status_var.set(f"Found card: {node.get('name', 'Unknown')}. Proceeding...")
            download_selected(node)
        else:
            status_var.set(f"Multiple cards found ({count}). Awaiting selection...")
            def download_callback(node):
                # Run in a thread so the popup stays responsive for more picks.
                threading.Thread(target=download_selected, args=(node,), daemon=True).start()
            regular_search_params = {
                'search': name, 'nsfw': 'true', 'nsfl': 'true', 'count': 'true'
            }
            popup = CardSelectionPopup(app, name, initial_response, headers, download_callback=download_callback, search_params=regular_search_params)

    except requests.exceptions.RequestException as e:
        status_var.set(f"API Error: {e}")
        messagebox.showerror("API Error", f"An error occurred while communicating with the API: {e}")
    except Exception as err:
        logging.error(f"An error occurred: {err}")
        messagebox.showerror("Error", f"An error occurred: {err}")
        status_var.set("Error occurred. Ready for new attempt.")
    finally:
        # Always re-enable UI
        app.after(0, set_ui_state, NORMAL)

def download_all_own_cards():
    """Download every card owned by the logged-in user, including private ones.

    Requires the Chub session token (set via 'Set Chub.ai Token'), because the
    authenticated /api/users/{creator}?include_projects=true endpoint is the
    only way to see your own private/unlisted cards.
    """
    api_token = config['Settings'].get('api_token', '').strip()
    if not api_token:
        messagebox.showwarning(
            "Session Token Required",
            "Downloading your own bots (including private ones) requires your "
            "Chub session token.\n\n"
            "To set it:\n"
            "1. Log in to chub.ai in your browser.\n"
            "2. Open DevTools (F12) -> Application -> Cookies -> https://chub.ai.\n"
            "3. Copy the value of the 'session' cookie.\n"
            "4. Click 'Set Chub.ai Token' here and paste it.\n\n"
            "Then try again.",
        )
        status_var.set("Session token not set. Cannot download own bots.")
        return

    output_directory = output_dir.get()
    if not output_directory:
        messagebox.showwarning("Output Directory Not Set", "Please select an output directory.")
        return

    bundle_option = var.get()
    headers = {
        'accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    if api_token:
        headers['Authorization'] = f'Bearer {api_token}'

    try:
        # Verify the token and resolve the creator's username from /api/self.
        status_var.set("Verifying session token...")
        account = chub_stats_server.fetch_account_info(api_token)
        if not account.get('authenticated'):
            messagebox.showerror(
                "Token Not Valid",
                "Your session token could not be verified. Please re-set it via "
                "'Set Chub.ai Token'.\n\n"
                "Make sure you copied the 'session' cookie value (not the auth "
                "header), and that you are still logged in on chub.ai.",
            )
            status_var.set("Token verification failed. Ready.")
            return

        creator = account.get('user_name') or account.get('name')
        if not creator:
            messagebox.showerror("Account Error", "Could not determine your username from the account info.")
            status_var.set("Could not resolve account username. Ready.")
            return

        # Fetch all the user's cards (includes private/unlisted when authenticated).
        status_var.set(f"Fetching bot list for '{creator}'...")
        nodes = chub_stats_server.fetch_all_cards(api_token, creator)
        if not nodes:
            messagebox.showinfo("No Bots Found", f"No bots were found for the account '{creator}'.")
            status_var.set("No own bots found. Ready.")
            return

        # The user-projects endpoint can also return lorebooks / presets / other
        # project types that aren't character cards. Keep only characters.
        total_projects = len(nodes)
        nodes = [
            n for n in nodes
            if (n.get('projectSpace') or 'characters').lower() == 'characters'
        ]
        skipped_non_chars = total_projects - len(nodes)
        if skipped_non_chars:
            logging.info(f"Filtered out {skipped_non_chars} non-character project(s) (lorebooks/presets/etc).")

        if not nodes:
            messagebox.showinfo("No Bots Found", f"No character bots were found for the account '{creator}'.")
            status_var.set("No own character bots found. Ready.")
            return

        # Confirm before downloading a potentially large batch.
        confirm = messagebox.askyesno(
            "Download All Own Bots",
            f"Found {len(nodes)} character bot(s) for '{creator}'.\n\nDownload all of them to:\n{output_directory}\n?",
        )
        if not confirm:
            status_var.set("Download cancelled. Ready.")
            return

        # Pre-scan: detect cards whose destination (folder or zip, depending on
        # the selected bundle option) already exists in the output directory.
        existing = []
        for node in nodes:
            sanitized_name = sanitize_filename(node.get('name', 'Unknown'))
            if bundle_option == 'Zip':
                dest = os.path.join(output_directory, f"{sanitized_name}.zip")
            else:
                dest = os.path.join(output_directory, sanitized_name)
            if os.path.exists(dest):
                existing.append(node.get('name', 'Unknown'))

        # Ask once how to handle already-downloaded cards.
        # skip_existing=True -> skip them; False -> re-download (overwrite).
        skip_existing = False
        if existing:
            choice = messagebox.askyesnocancel(
                "Existing Cards Found",
                f"{len(existing)} of {len(nodes)} card(s) already exist in the output directory:\n- "
                + "\n- ".join(existing[:10])
                + ("\n... (and {} more)".format(len(existing) - 10) if len(existing) > 10 else "")
                + "\n\nYes = skip existing cards\nNo = overwrite existing cards\nCancel = abort download",
            )
            if choice is None:
                status_var.set("Download cancelled. Ready.")
                return
            skip_existing = bool(choice)

        success = 0
        failed = []
        skipped = 0
        for i, node in enumerate(nodes, start=1):
            card_name = node.get('name', 'Unknown')
            # Short name for status display: first 10 chars, with "..." if truncated.
            short_name = card_name[:10] + ("..." if len(card_name) > 10 else "")
            sanitized_name = sanitize_filename(card_name)
            if bundle_option == 'Zip':
                dest = os.path.join(output_directory, f"{sanitized_name}.zip")
            else:
                dest = os.path.join(output_directory, sanitized_name)

            # Honor the skip/overwrite choice from the pre-scan.
            if skip_existing and os.path.exists(dest):
                logging.info(f"Skipping '{card_name}' (already exists at {dest}).")
                skipped += 1
                status_var.set(f"DL: {i}/{len(nodes)} {short_name} Skipping (already exists)...")
                continue

            # Prefix prepended to every status line for this card so the user can
            # see which bot is being processed within the overall batch.
            batch_prefix = f"DL: {i}/{len(nodes)} {short_name} "
            status_var.set(f"{batch_prefix}Downloading...")
            try:
                ai_rating, _ = determine_ai_rating(node, headers, status_var)
                download_card_thread(node, bundle_option, output_directory, headers, ai_rating, status_var, suppress_popup=True, batch_prefix=batch_prefix)
                success += 1
            except Exception as exc:
                logging.error(f"Failed to download '{card_name}': {exc}")
                failed.append(card_name)
            # Throttle between cards to avoid read timeouts / rate limits.
            if i < len(nodes):
                time.sleep(API_CALL_DELAY)

        if failed:
            messagebox.showwarning(
                "Download Complete (with errors)",
                f"Downloaded {success}/{len(nodes)} bot(s).\nSkipped: {skipped}\n\nFailed:\n- " + "\n- ".join(failed),
            )
        else:
            messagebox.showinfo("Download Complete", f"Successfully downloaded {success} bot(s).\nSkipped: {skipped}.")
        status_var.set(f"Downloaded {success}/{len(nodes)} own bots (skipped {skipped}). Ready.")

    except requests.exceptions.RequestException as e:
        logging.error(f"Network error while downloading own bots: {e}")
        messagebox.showerror("API Error", f"Failed to communicate with Chub.ai API: {e}")
        status_var.set("API error. Ready.")
    except Exception as err:
        logging.error(f"Error downloading own bots: {err}")
        messagebox.showerror("Error", f"An error occurred: {err}")
        status_var.set("Error occurred. Ready.")

def on_download_all_own_click():
    set_ui_state(DISABLED)
    threading.Thread(target=download_all_own_cards, daemon=True).start()

def download_card():
    """
    Initiates the download process in a separate thread.
    """
    threading.Thread(target=download_card_thread).start()

def generate_html(node, gallery_images=None):
    """
    Generates an HTML file with card information and description.
    gallery_images: optional list of local image filenames (in the same folder
    as this HTML file) to render as an interactive gallery.
    """
    # Convert markdown description to HTML
    description_html = markdown.markdown(node.get('description', ''))

    # Parse TOKEN_COUNTS
    token_counts = {}
    for label in node.get('labels', []):
        if label.get('title') == 'TOKEN_COUNTS':
            try:
                token_data = json.loads(label.get('description', '{}'))
                for key, value in token_data.items():
                    if value != 0 and key != 'total':
                        # Make the key more readable
                        key_readable = key.replace('_', ' ').title()
                        token_counts[key_readable] = value
                break  # Break after finding TOKEN_COUNTS
            except json.JSONDecodeError:
                pass  # Ignore if JSON is invalid

    # Prepare other fields with friendly labels
    fields = {
        'Name': node.get('name', ''),
        'ID': node.get('id', ''),
        'Full Path': node.get('fullPath', ''),
        'Downloads': node.get('starCount', ''),
        'Last Activity': node.get('lastActivityAt', ''),
        'Created At': node.get('createdAt', ''),
        'Tags': ', '.join(node.get('topics', [])),
        'Forks': node.get('forksCount', ''),
        'Rating': node.get('rating', ''),
        'Rating Count': node.get('ratingCount', ''),
        'Tagline': node.get('tagline', ''),
        'Chats': node.get('nChats', ''),
        'Messages': node.get('nMessages', ''),
        'Public Chats': node.get('n_public_chats', ''),
        'Favorites': node.get('n_favorites', ''),
        'Avatar URL': node.get('avatar_url', ''),
    }

    # HTML template with updated colors and responsive layout
    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{fields['Name']} - Card Information</title>
        <style>
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                margin: 0;
                padding: 0;
                background-color: #f4f4f4;
            }}
            .container {{
                max-width: 1200px;
                margin: 40px auto;
                background-color: #fff;
                padding: 30px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                border-radius: 8px;
            }}
            h1 {{
                text-align: center;
                margin-bottom: 20px;
                color: {highlight_color};
            }}
            .avatar {{
                display: block;
                margin-left: auto;
                margin-right: auto;
                width: 200px;
                height: 200px;
                border-radius: 50%;
                object-fit: cover;
                box-shadow: 0 2px 5px rgba(0,0,0,0.2);
            }}
            .description {{
                margin-top: 30px;
            }}
            .description h2 {{
                border-bottom: 2px solid #e7e7e7;
                padding-bottom: 10px;
                color: {highlight_color};
            }}
            .description p {{
                line-height: 1.8;
                color: #555;
            }}
            .info {{
                margin-top: 30px;
            }}
            .info h2 {{
                border-bottom: 2px solid #e7e7e7;
                padding-bottom: 10px;
                color: {highlight_color};
            }}
            .info ul {{
                list-style-type: none;
                padding: 0;
                display: grid;
                grid-template-columns: 1fr;
                gap: 10px;
            }}
            .info ul li {{
                background: #fafafa;
                padding: 12px 15px;
                border-radius: 5px;
                display: flex;
                flex-direction: column;
                word-wrap: break-word;
            }}
            .info ul li strong {{
                color: #333;
                margin-bottom: 5px;
            }}
            .token-counts {{
                margin-top: 30px;
            }}
            .token-counts h2 {{
                border-bottom: 2px solid #e7e7e7;
                padding-bottom: 10px;
                color: {highlight_color};
            }}
            .token-counts ul {{
                list-style-type: none;
                padding: 0;
                display: grid;
                grid-template-columns: 1fr;
                gap: 10px;
            }}
            .token-counts ul li {{
                background: #eaf8fc;
                padding: 12px 15px;
                border-radius: 5px;
                display: flex;
                flex-direction: column;
                word-wrap: break-word;
            }}
            footer {{
                text-align: center;
                margin-top: 40px;
                color: #aaa;
            }}
            @media (min-width: 500px) {{
                .info ul {{
                    grid-template-columns: 1fr 1fr;
                }}
                .token-counts ul {{
                    grid-template-columns: 1fr 1fr;
                }}
            }}
            @media (min-width: 800px) {{
                .info ul {{
                    grid-template-columns: 1fr 1fr 1fr;
                }}
                .token-counts ul {{
                    grid-template-columns: 1fr 1fr 1fr;
                }}
            }}
            .gallery {{
                margin-top: 30px;
            }}
            .gallery h2 {{
                border-bottom: 2px solid #e7e7e7;
                padding-bottom: 10px;
                color: {highlight_color};
            }}
            .gallery-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
                gap: 10px;
                margin-top: 15px;
            }}
            .gallery-grid figure {{
                margin: 0;
                cursor: pointer;
                border-radius: 6px;
                overflow: hidden;
                background: #fafafa;
                box-shadow: 0 1px 3px rgba(0,0,0,0.08);
                transition: transform 0.12s ease;
            }}
            .gallery-grid figure:hover {{
                transform: scale(1.03);
            }}
            .gallery-grid img {{
                width: 100%;
                height: 140px;
                object-fit: cover;
                display: block;
            }}
            .lightbox {{
                display: none;
                position: fixed;
                inset: 0;
                background: rgba(0,0,0,0.85);
                z-index: 9999;
                justify-content: center;
                align-items: center;
                padding: 30px;
            }}
            .lightbox.active {{
                display: flex;
            }}
            .lightbox img {{
                max-width: 90vw;
                max-height: 85vh;
                border-radius: 6px;
                box-shadow: 0 4px 20px rgba(0,0,0,0.5);
            }}
            .lightbox-close {{
                position: absolute;
                top: 18px;
                right: 28px;
                color: #fff;
                font-size: 36px;
                cursor: pointer;
                user-select: none;
                line-height: 1;
            }}
            .lightbox-nav {{
                position: absolute;
                top: 50%;
                transform: translateY(-50%);
                color: #fff;
                font-size: 48px;
                cursor: pointer;
                user-select: none;
                padding: 16px;
                line-height: 1;
                opacity: 0.7;
            }}
            .lightbox-nav:hover {{
                opacity: 1;
            }}
            .lightbox-prev {{ left: 10px; }}
            .lightbox-next {{ right: 10px; }}
            .lightbox-counter {{
                position: absolute;
                bottom: 20px;
                left: 50%;
                transform: translateX(-50%);
                color: #fff;
                font-size: 14px;
                background: rgba(0,0,0,0.5);
                padding: 6px 14px;
                border-radius: 12px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>{fields['Name']}</h1>
            <img src="{fields['Avatar URL']}" alt="Avatar" class="avatar">
            <div class="description">
                <h2>Description</h2>
                {description_html}
            </div>
            <div class="info">
                <h2>Card Information</h2>
                <ul>
    """

    # Add card information items
    for key, value in fields.items():
        if key not in ['Name', 'Description', 'Avatar URL', 'ai_rating_determined']:
            html_template += f"""
                        <li>
                            <strong>{key}:</strong>
                            <span>{value}</span>
                        </li>
            """
    # Specifically add determined AI rating if available
    determined_ai_rating_value = node.get('ai_rating_determined') # Get from the input node directly
    determined_ai_rating_display = "Not Rated" # Default display

    if determined_ai_rating_value is not None:
        if determined_ai_rating_value == 0:
            determined_ai_rating_display = "Not Rated"
        else:
            determined_ai_rating_display = str(determined_ai_rating_value)
        
        html_template += f"""
                        <li>
                            <strong>AI Rating (Determined):</strong> {determined_ai_rating_display}
                        </li>
        """

    html_template += """
                </ul>
            </div>
    """

    # Add token counts if they exist
    if token_counts:
        html_template += f"""
            <div class="token-counts">
                <h2>Token Counts</h2>
                <ul>
        """
        for key, value in token_counts.items():
            html_template += f"""
                        <li>
                            <strong>{key}:</strong>
                            <span>{value}</span>
                        </li>
            """
        html_template += """
                </ul>
            </div>
        """

    # Add interactive gallery if gallery images were downloaded.
    if gallery_images:
        # Escape filenames for safe embedding in HTML/JS.
        thumbnails_html = "\n".join(
            f'                <figure data-index="{i}"><img src="{fn}" alt="Gallery image {i+1}" loading="lazy"></figure>'
            for i, fn in enumerate(gallery_images)
        )
        # JSON-encode the list so JS gets a clean array (handles quoting/escaping).
        images_json = json.dumps(gallery_images)
        gallery_count = len(gallery_images)
        html_template += f"""
            <div class="gallery">
                <h2>Gallery ({gallery_count})</h2>
                <div class="gallery-grid">
{thumbnails_html}
                </div>
            </div>
            <div class="lightbox" id="lightbox">
                <span class="lightbox-close" id="lightbox-close">&times;</span>
                <span class="lightbox-nav lightbox-prev" id="lightbox-prev">&#8249;</span>
                <img id="lightbox-img" src="" alt="">
                <span class="lightbox-nav lightbox-next" id="lightbox-next">&#8250;</span>
                <span class="lightbox-counter" id="lightbox-counter"></span>
            </div>
            <script>
                (function() {{
                    const images = {images_json};
                    let current = 0;
                    const box = document.getElementById('lightbox');
                    const boxImg = document.getElementById('lightbox-img');
                    const counter = document.getElementById('lightbox-counter');

                    function show(index) {{
                        current = (index + images.length) % images.length;
                        boxImg.src = images[current];
                        counter.textContent = (current + 1) + ' / ' + images.length;
                    }}

                    document.querySelectorAll('.gallery-grid figure').forEach(fig => {{
                        fig.addEventListener('click', () => {{
                            show(parseInt(fig.dataset.index, 10));
                            box.classList.add('active');
                        }});
                    }});

                    function close() {{ box.classList.remove('active'); }}
                    document.getElementById('lightbox-close').addEventListener('click', close);
                    box.addEventListener('click', e => {{ if (e.target === box) close(); }});

                    document.getElementById('lightbox-prev').addEventListener('click', e => {{ e.stopPropagation(); show(current - 1); }});
                    document.getElementById('lightbox-next').addEventListener('click', e => {{ e.stopPropagation(); show(current + 1); }});

                    document.addEventListener('keydown', e => {{
                        if (!box.classList.contains('active')) return;
                        if (e.key === 'Escape') close();
                        if (e.key === 'ArrowLeft') show(current - 1);
                        if (e.key === 'ArrowRight') show(current + 1);
                    }});
                }})();
            </script>
        """

    html_template += f"""
        </div>
        <footer>
            Generated by Chub.ai Card Downloader
        </footer>
    </body>
    </html>
    """

    return html_template

# GUI Setup
frame = ttk.Frame(app, padding=10)
frame.pack(fill=BOTH, expand=YES)

# Use grid layout for better control
frame.columnconfigure(1, weight=1)

# Card Name Entry
label = ttk.Label(frame, text="Card Name:")
label.grid(row=0, column=0, sticky=W, pady=(5, 5))

entry = ttk.Entry(frame)
entry.grid(row=0, column=1, sticky=EW, pady=(5, 5), columnspan=2)

# Bundle Option
option_label = ttk.Label(frame, text="Bundle As:")
option_label.grid(row=1, column=0, sticky=W, pady=(5, 5))

var = ttk.StringVar(value=config['Settings']['bundle_option'])

options = ['Folder', 'Zip']
option_menu = ttk.Combobox(frame, textvariable=var, values=options, state='readonly', width=10)
option_menu.grid(row=1, column=1, sticky=W, pady=(5, 5))
option_menu.current(options.index(config['Settings']['bundle_option']))

def on_option_change(*args):
    config['Settings']['bundle_option'] = var.get()
    save_config()

var.trace_add('write', on_option_change)

# Output Directory
output_label = ttk.Label(frame, text="Output Directory:")
output_label.grid(row=2, column=0, sticky=W, pady=(5, 5))

output_dir = ttk.StringVar(value=config['Settings']['output_directory'])

output_dir_entry = ttk.Entry(frame, textvariable=output_dir, state='readonly')
output_dir_entry.grid(row=2, column=1, sticky=EW, pady=(5, 5))

def select_output_directory():
    directory = filedialog.askdirectory(title="Select Output Directory")
    if directory:
        output_dir.set(directory)
        config['Settings']['output_directory'] = directory
        save_config()

select_output_button = ttk.Button(frame, text="Browse", command=select_output_directory, style='Custom.TButton')
select_output_button.grid(row=2, column=2, sticky=W, padx=(5, 0), pady=(5, 5))

# Set Chub.ai Token Button + Download All Own Bots Button (share row 3)
token_button = ttk.Button(frame, text="Set Chub.ai Token", command=set_api_token, style='Custom.TButton')
token_button.grid(row=3, column=0, columnspan=2, sticky=EW, pady=(10, 0))

download_all_own_button = ttk.Button(frame, text="Download All Own Bots", command=on_download_all_own_click, style='Custom.TButton')
download_all_own_button.grid(row=3, column=2, sticky=EW, padx=(5, 0), pady=(10, 0))

# Search and Download Buttons Frame
buttons_frame = ttk.Frame(frame)
buttons_frame.grid(row=4, column=0, columnspan=3, sticky=EW, pady=(10, 0))
buttons_frame.grid_columnconfigure(0, weight=1)

# Download by URL/Path Button
download_url_button = ttk.Button(buttons_frame, text="Download by URL/Path", command=on_download_click, style='Custom.TButton')
download_url_button.grid(row=0, column=0, sticky=EW, padx=(0, 5))

# Search and Advanced Buttons
search_buttons_subframe = ttk.Frame(buttons_frame)
search_buttons_subframe.grid(row=0, column=1, sticky=EW)
search_buttons_subframe.grid_columnconfigure(0, weight=1)
search_buttons_subframe.grid_columnconfigure(1, weight=1)

search_button = ttk.Button(search_buttons_subframe, text="Search", command=on_search_click, style='Custom.TButton')
search_button.grid(row=0, column=0, sticky=EW, padx=(0, 2))

advanced_search_button = ttk.Button(search_buttons_subframe, text="Advanced", command=open_advanced_search, style='Custom.TButton')
advanced_search_button.grid(row=0, column=1, sticky=EW, padx=(2, 0))

# Dynamically adjust the column weights
app.after(100, lambda: buttons_frame.grid_columnconfigure(1, weight=download_url_button.winfo_width()))

# Status Bar
status_var = tk.StringVar()
status_var.set('Ready')

def set_ui_state(state):
    # state should be NORMAL or DISABLED
    search_button.config(state=state)
    advanced_search_button.config(state=state)
    download_url_button.config(state=state)
    download_all_own_button.config(state=state)
status_bar = ttk.Label(app, textvariable=status_var, relief=SUNKEN, anchor=W, font=('Segoe UI', 10))
status_bar.pack(side=BOTTOM, fill=X)

# Version label in lower right corner
version_label = ttk.Label(app, text="v1.8.1", font=('Segoe UI', 8))
version_label.place(relx=1.0, rely=1.0, x=-5, y=-5, anchor='se')

# Run the application
app.mainloop()
