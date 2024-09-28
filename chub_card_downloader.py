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

# Configure logging
logging.basicConfig(filename='error.log', level=logging.ERROR, 
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
config_file = 'config.ini'

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
            'User-Agent': 'ChubCardDownloader/1.0'
        }
        if api_token:
            headers['Authorization'] = f'Bearer {api_token}'

        # First API call: Search for the card
        search_url = f"https://api.chub.ai/api/characters/search?search={name}&nsfw=true&nsfl=true&first=100&page=1&sort=created_at&asc=false"

        response = requests.get(search_url, headers=headers)
        response.raise_for_status()

        data = response.json()

        count = data.get('count', 0)
        if count == 0:
            if not api_token:
                messagebox.showinfo(
                    "No Results",
                    "No card found with the given name.\n\n"
                    "If you're searching for NSFL or private cards, you may need to set your Chub.ai token."
                )
            else:
                messagebox.showinfo("No Results", "No card found with the given name.")
            return
        elif count > 1:
            messagebox.showinfo("Multiple Results", "Multiple cards found. Please enter a more specific name or the card code.")
            return

        node = data['nodes'][0]
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

        # Second API call to download PNG
        download_url = "https://api.chub.ai/api/characters/download"
        payload = {
            "format": "card_spec_v2",
            "fullPath": full_path,
            "version": "main"
        }

        download_headers = headers.copy()
        download_headers['accept'] = '*/*'
        download_headers['Content-Type'] = 'application/json'

        response = requests.post(download_url, headers=download_headers, json=payload)
        response.raise_for_status()

        # Save the PNG file directly without using PIL to preserve metadata
        with open(os.path.join(card_dir, f"{sanitized_name}.png"), 'wb') as img_file:
            img_file.write(response.content)

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
        else:
            messagebox.showinfo("Success", f"All files have been saved in {card_dir}")

    except requests.exceptions.HTTPError as http_err:
        logging.error(f"HTTP error occurred: {http_err}")
        messagebox.showerror("HTTP Error", f"An HTTP error occurred: {http_err}")
    except Exception as err:
        logging.error(f"An error occurred: {err}")
        messagebox.showerror("Error", f"An error occurred: {err}")
    finally:
        # Re-enable buttons after download
        download_button.config(state=NORMAL)
        token_button.config(state=NORMAL)
        select_output_button.config(state=NORMAL)
        status_var.set("Ready")

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
        if key not in ['Name', 'Description', 'Avatar URL']:
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
