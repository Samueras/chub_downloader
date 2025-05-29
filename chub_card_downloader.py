import requests
import json
import os
import configparser
from io import BytesIO
import zipfile
import markdown  # For markdown conversion

# Import ttkbootstrap and tkinter modules
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from tkinter import messagebox, filedialog
import threading
import logging
import re  # Import regular expressions module
import tkinter as tk # For Canvas widget
from PIL import Image, ImageTk

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
error_log_file = os.path.join(application_path, 'error.log')
logging.basicConfig(filename=error_log_file, level=logging.ERROR, 
                    format='%(asctime)s:%(levelname)s:%(message)s')

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

# Load configuration
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

def _check_card_rating_api(full_path_query, target_card_id, min_rating_to_check, headers, status_var, current_api_call_num):
    """Helper function to check if a card appears in search with a given min_ai_rating."""
    if status_var:
        status_var.set(f"Checking AI rating... (Call {current_api_call_num}/{MAX_AI_RATING_API_CALLS}, rating >= {min_rating_to_check})")
    
    # Search by target_card_id for AI rating check, include nsfw=true and nsfl=true
    search_url = f"https://api.chub.ai/search?search={target_card_id}&min_ai_rating={min_rating_to_check}&nsfw=true&nsfl=true"
    try:
        response = requests.get(search_url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        nodes = data.get('data', {}).get('nodes', [])
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

def determine_ai_rating(selected_node_data, headers, status_var=None):
    """Determines the AI rating of a card using binary search on the search API."""
    full_path = selected_node_data.get('fullPath')
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


class CardSelectionPopup(ttk.Toplevel):
    def __init__(self, parent, cards_data):
        super().__init__(parent)
        self.title("Select a Card")
        self.geometry("650x550") # Adjusted size
        self.parent = parent
        self.cards_data = cards_data
        self.selected_card_node = None
        self.scrollable_frame = None # Initialize scrollable_frame
        self.select_buttons = [] # To store select buttons
        # self.style = app_style # This line is removed as it's not needed and causes the error

        # Make the popup modal
        self.transient(parent) # Set to be on top of the parent
        self.grab_set() # Direct all events to this window

        self.create_widgets()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def create_widgets(self):
        container_frame = ttk.Frame(self, padding=10)
        container_frame.pack(fill=BOTH, expand=YES)

        # Add a Canvas for scrolling
        canvas = tk.Canvas(container_frame, borderwidth=0, background="#ffffff") # Use tk.Canvas
        scrollbar = ttk.Scrollbar(container_frame, orient="vertical", command=canvas.yview)
        self.scrollable_frame = ttk.Frame(canvas) # Use ttk.Frame inside canvas, assign to self

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(
                scrollregion=canvas.bbox("all")
            )
        )

        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw") # Use self.scrollable_frame
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)
        
        if not self.cards_data:
            ttk.Label(self.scrollable_frame, text="No cards to display.").pack(padx=10, pady=10)
            return

        MAX_CARDS_TO_DISPLAY = 20
        displayed_cards = self.cards_data[:MAX_CARDS_TO_DISPLAY]

        for i, card_node in enumerate(displayed_cards):
            card_item_frame = ttk.Frame(self.scrollable_frame, padding=10, relief=SOLID, borderwidth=1)
            card_item_frame.pack(pady=10, padx=10, fill=X, expand=YES)

            # --- Left side: Image ---
            left_frame = ttk.Frame(card_item_frame)
            left_frame.pack(side=LEFT, padx=(0,10), fill=Y)

            avatar_url = card_node.get('avatar_url')
            img_data = None
            if avatar_url:
                try:
                    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
                    response = requests.get(avatar_url, headers=headers, stream=True, timeout=10)
                    response.raise_for_status()
                    img_data = response.content
                except requests.exceptions.RequestException as e:
                    print(f"Error fetching avatar {avatar_url}: {e}")
                    img_data = None

            if img_data:
                try:
                    image = Image.open(BytesIO(img_data))
                    image.thumbnail((100, 100)) # Resize to max 100x100
                    photo = ImageTk.PhotoImage(image)
                    img_label = ttk.Label(left_frame, image=photo)
                    img_label.image = photo # Keep a reference!
                    img_label.pack(pady=5, padx=5)
                except Exception as e:
                    print(f"Error processing image for {card_node.get('name')}: {e}")
                    ttk.Label(left_frame, text="Image N/A").pack(pady=5, padx=5)
            else:
                ttk.Label(left_frame, text="No Avatar").pack(pady=5, padx=5)

            # --- Right side: Details and Button ---
            right_frame = ttk.Frame(card_item_frame)
            right_frame.pack(side=LEFT, fill=X, expand=YES)

            name_label = ttk.Label(right_frame, text=f"{card_node.get('name', 'N/A')}", font=('Segoe UI', 12, 'bold'), wraplength=450)
            name_label.pack(anchor=W, pady=(0,2))
            
            path_label = ttk.Label(right_frame, text=f"Path: {card_node.get('fullPath', 'N/A')}", font=('Segoe UI', 9), wraplength=450)
            path_label.pack(anchor=W)

            tagline_text = card_node.get('tagline', 'N/A')
            if not tagline_text or tagline_text.isspace():
                tagline_text = "No tagline available."
            tagline_label = ttk.Label(right_frame, text=f"{tagline_text}", font=('Segoe UI', 10), wraplength=450, justify=LEFT)
            tagline_label.pack(anchor=W, pady=(5,10), fill=X, expand=YES)

            select_button = ttk.Button(right_frame, text="Select this Card", 
                                       command=lambda cn=card_node: self.on_select(cn), style='Custom.TButton', state=DISABLED)
            select_button.pack(anchor=E, pady=5)
            self.select_buttons.append(select_button)

        if len(self.cards_data) > MAX_CARDS_TO_DISPLAY:
            info_label = ttk.Label(self.scrollable_frame, 
                                   text=f"Showing {MAX_CARDS_TO_DISPLAY} of {len(self.cards_data)} results. Refine search if needed.", 
                                   font=('Segoe UI', 9, 'italic'))
            info_label.pack(pady=(10,5), padx=10, fill=X)

        # Enable all select buttons now that card items are loaded
        for btn in self.select_buttons:
            btn.config(state=NORMAL)

    def on_select(self, card_node):
        self.selected_card_node = card_node
        if self.scrollable_frame: 
            # Destroy all children of scrollable_frame first
            for widget in self.scrollable_frame.winfo_children():
                widget.destroy()
            self.scrollable_frame.unbind("<Configure>")
        self.grab_release()
        self.after_idle(self.destroy) # Defer destroy

    def on_close(self):
        self.selected_card_node = None # Explicitly set to None
        if self.scrollable_frame:
            # Destroy all children of scrollable_frame first
            for widget in self.scrollable_frame.winfo_children():
                widget.destroy()
            self.scrollable_frame.unbind("<Configure>")
        self.grab_release()
        self.after_idle(self.destroy) # Defer destroy

    def show(self):
        self.parent.wait_window(self) # Wait for this window to close
        return self.selected_card_node

def download_card_thread():
    """
    Handles the card download process in a separate thread.
    Provides feedback and error handling.
    """
    try:
        # Disable buttons during download
        download_button.config(state=DISABLED)
        token_button.config(state=DISABLED)
        select_output_button.config(state=DISABLED)
        status_var.set("Downloading card...")

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

        # New API call: Search for the card, include nsfw=true and nsfl=true
        search_url = f"https://api.chub.ai/search?search={name}&nsfw=true&nsfl=true"
        print(f"Initial search URL: {search_url}") # Debug print for initial search
        status_var.set(f"Searching for: {name}...")

        response = requests.get(search_url, headers=headers)
        response.raise_for_status()

        api_response_data = response.json()

        nodes = api_response_data.get('data', {}).get('nodes', [])
        count = len(nodes)

        node = None # Initialize node
        if count == 0:
            if not api_token: # This check's relevance might change if token isn't used for search
                messagebox.showinfo(
                    "No Results",
                    "No card found with the given name.\n\n"
                    "If you're searching for NSFL or private cards, you may need to set your Chub.ai token."
                )
            else:
                messagebox.showinfo("No Results", "No card found with the given name.")
            status_var.set("No results found. Ready.")
            return # Essential to stop processing if no card is found
        elif count == 1:
            node = nodes[0]
            status_var.set(f"Found card: {node.get('name', 'Unknown')}. Proceeding...")
        else:  # count > 1
            status_var.set(f"Multiple cards found ({count}). Awaiting selection...")
            # 'app' is a global variable in this script's context. 'style' is also global for ttk.
            popup = CardSelectionPopup(app, nodes) 
            selected_card_from_popup = popup.show() # This blocks until popup is closed

            if selected_card_from_popup:
                node = selected_card_from_popup
                status_var.set(f"Card selected: {node.get('name', 'Unknown')}. Proceeding...")
            else:
                status_var.set("Card selection cancelled. Ready.")
                messagebox.showinfo("Selection Cancelled", "No card was selected for download.")
                return # Exit if no card selected

        # Check if a card was actually selected/found before proceeding
        if not node:
            status_var.set("No card available for download. Ready.")
            # messagebox.showinfo("Process Halted", "No card was available or selected for download.") # Already handled by selection logic
            return

        # Determine AI Rating
        status_var.set(f"Preparing to determine AI rating for {node.get('name', 'Unknown')}...")
        ai_rating, calls_made = determine_ai_rating(node, headers, status_var)
        node['ai_rating_determined'] = ai_rating # Store it in the node data for HTML generation
        # messagebox.showinfo("AI Rating Check", f"Determined AI Rating for '{node.get('name', 'Unknown')}': {ai_rating}\n(Took {calls_made} API calls)")

        card_id = node['id']
        full_path = node['fullPath']
        description = node['description']
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

        # Save description and additional information as HTML using markdown and a template
        html_content = generate_html(node)
        with open(os.path.join(card_dir, f"{sanitized_name}_info.html"), 'w', encoding='utf-8') as f:
            f.write(html_content)

        # Download PNG using max_res_url from the search result node
        max_res_url = node.get('max_res_url')
        if not max_res_url:
            messagebox.showerror("Download Error", "Could not find 'max_res_url' for the selected card to download the image.")
            # Optionally, decide if you want to proceed without the main image or return
            # For now, let's log and potentially allow proceeding for gallery/HTML
            logging.error(f"max_res_url not found for card {full_path}")
            # If the main image is critical, you might want to 'return' here.
        else:
            status_var.set(f"Downloading card image from {max_res_url[:50]}...")
            try:
                # Use the same headers as search, or a simplified one if token not needed for direct image download
                image_response = requests.get(max_res_url, headers=headers, stream=True, timeout=30)
                image_response.raise_for_status()
                
                # Save the PNG file
                with open(os.path.join(card_dir, f"{sanitized_name}.png"), 'wb') as img_file:
                    for chunk in image_response.iter_content(chunk_size=8192):
                        img_file.write(chunk)
                status_var.set("Card image downloaded.")
            except requests.exceptions.RequestException as img_err:
                messagebox.showerror("Image Download Error", f"Failed to download card image from {max_res_url}: {img_err}")
                logging.error(f"Failed to download card image from {max_res_url}: {img_err}")
                # Decide if you want to proceed or return here as well

        # Third API call to get gallery images
        gallery_url = f"https://api.chub.ai/api/gallery/project/{card_id}?nsfw=true&page=1&limit=24"

        response = requests.get(gallery_url, headers=headers)
        response.raise_for_status()

        gallery_data = response.json()
        gallery_count = gallery_data.get('count', 0)

        if gallery_count >= 1:
            for image_node in gallery_data['nodes']:
                image_url = image_node['primary_image_path']
                image_response = requests.get(image_url)
                if image_response.status_code == 200:
                    image_name = image_url.split('/')[-1]
                    sanitized_image_name = sanitize_filename(image_name)
                    with open(os.path.join(card_dir, sanitized_image_name), 'wb') as img_file:
                        img_file.write(image_response.content)
                else:
                    logging.error(f"Failed to download gallery image: {image_url}")
        else:
            messagebox.showinfo("Gallery Info", "No gallery images found.")

        # Bundle option
        if bundle_option == 'Zip':
            zipf = zipfile.ZipFile(f"{card_dir}.zip", 'w', zipfile.ZIP_DEFLATED)
            for root, dirs, files in os.walk(card_dir):
                for file in files:
                    zipf.write(os.path.join(root, file), arcname=file)
            zipf.close()
            # Remove the folder if zipped
            for root, dirs, files in os.walk(card_dir, topdown=False):
                for file in files:
                    os.remove(os.path.join(root, file))
                os.rmdir(root)
            messagebox.showinfo("Success", f"All files have been saved and zipped at {card_dir}.zip")
            status_var.set("Download complete. Ready.")
        else:
            messagebox.showinfo("Success", f"All files have been saved in {card_dir}")
            status_var.set("Download complete. Ready.")

    except requests.exceptions.HTTPError as http_err:
        logging.error(f"HTTP error occurred: {http_err}")
        messagebox.showerror("HTTP Error", f"An HTTP error occurred: {http_err}")
        status_var.set("HTTP Error. Ready for new attempt.")
    except Exception as err:
        logging.error(f"An error occurred: {err}")
        messagebox.showerror("Error", f"An error occurred: {err}")
        status_var.set("Error occurred. Ready for new attempt.")
    finally:
        # Re-enable buttons after download or cancellation/error
        download_button.config(state=NORMAL)
        token_button.config(state=NORMAL)
        select_output_button.config(state=NORMAL)
        # Status is set by specific paths (success, error, cancellation), so no general set here.

def download_card():
    """
    Initiates the download process in a separate thread.
    """
    threading.Thread(target=download_card_thread).start()

def generate_html(node):
    """
    Generates an HTML file with card information and description.
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

# Set Chub.ai Token Button
token_button = ttk.Button(frame, text="Set Chub.ai Token", command=set_api_token, style='Custom.TButton')
token_button.grid(row=3, column=0, columnspan=3, sticky=EW, pady=(10, 0))

# Download Button
download_button = ttk.Button(frame, text="Download Card", command=download_card, style='Custom.TButton')
download_button.grid(row=4, column=0, columnspan=3, sticky=EW, pady=(10, 0))

# Status Bar
status_var = ttk.StringVar(value="Ready")
status_bar = ttk.Label(app, textvariable=status_var, relief=SUNKEN, anchor=W, font=('Segoe UI', 10))
status_bar.pack(side=BOTTOM, fill=X)

# Run the application
app.mainloop()
