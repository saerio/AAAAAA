import json
import requests
import discord
from discord.ext import tasks
from discord.ext.commands import Bot
from discord import app_commands, ButtonStyle, ui, Color, Button, Embed
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image
import pytz
import aiohttp
import time
import random
import io
import os
# local imports
from depboard import NSStationInfoScraper

CONFIG_FILE = "config.json"
STATIONS_FILE = "stations.json"

intents = discord.Intents.default()
bot = Bot(command_prefix="!", intents=intents)
tree = bot.tree

# --- Global caches and configurations ---
STATIONS_CACHE = {}
STATIONS_LIST = []
TRAIN_TYPES_CACHE = set()
TRAIN_NUMBERS_CACHE = set()
word_prefix_map = {} # Added for efficient autocomplete

# Dictionary to store active departure boards per channel
# Format: {channel_id: {'station': station_name, 'message_id': None/int}}
active_departure_boards = {}

ANNOUNCED_TRAINS = {}  # Format: {ritId: {"timestamp": timestamp, "departure_time": departure_time}}

# --- Helper Functions ---
def load_stations():
    try:
        with open(STATIONS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data.get("payload", [])
    except Exception as e:
        print(f"Error loading stations: {e}")
        return []

def initialize_stations_cache():
    global STATIONS_CACHE, STATIONS_LIST, word_prefix_map
    stations = load_stations()

    long_names_map = {}
    word_prefix_map = {} # Re-initialize to ensure it's built correctly

    for station in stations:
        code = station.get("code")
        if not code:
            continue
        names = station.get("namen", {})
        long_name = names.get("lang")
        if long_name:
            long_names_map[code] = long_name

        for name_type in ["lang", "middel", "kort"]:
            name = names.get(name_type)
            if name:
                STATIONS_CACHE[name.lower()] = code

        for synonym in station.get("synoniemen", []):
            STATIONS_CACHE[synonym.lower()] = code

        STATIONS_CACHE[code.lower()] = code

    STATIONS_LIST = [long_names_map[code] for code in long_names_map if len(long_names_map[code]) > 1]
    STATIONS_LIST.sort()

    # Build prefix map for fast autocomplete
    for station_name in STATIONS_LIST:
        words = station_name.lower().split()
        for word in words:
            for i in range(1, len(word) + 1):
                prefix = word[:i]
                word_prefix_map.setdefault(prefix, set()).add(station_name)

    print(f"Loaded {len(STATIONS_CACHE)} station names and {len(STATIONS_LIST)} autocomplete entries")

def load_config():
    try:
        with open(CONFIG_FILE, "r") as file:
            return json.load(file)
    except Exception as e:
        print(f"Error loading config: {e}")
        return {}

def save_config(config):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        print(f"Error saving config: {e}")

def merge_images_horizontally(image_urls):
    # This function uses `requests` which is synchronous.
    # In an async bot, consider using `aiohttp` for image fetching too
    # to avoid blocking the event loop.
    images = [Image.open(BytesIO(requests.get(url).content)) for url in image_urls]
    total_width = sum(image.width for image in images)
    max_height = max(image.height for image in images)
    merged_image = Image.new("RGB", (total_width, max_height))
    current_width = 0
    for image in images:
        merged_image.paste(image, (current_width, 0))
        current_width += image.width
    img_byte_arr = BytesIO()
    merged_image.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr

async def send_discord_message_with_image(channel, **kwargs):
    OPERATOR_COLOR_MAP = {
        "NS": Color.gold(),
        "Arriva": Color.red(),
        "Breng": Color.purple(),
        "VIAS": Color.green(),
        "EUROSTAR": Color.blue(),
        "THALYS": Color.dark_red(),
        "ICE": Color.light_grey(),
    }
    train_type = kwargs.get("train_type", "Unknown")
    operator = kwargs.get("operator", "Unknown")

    color = OPERATOR_COLOR_MAP.get(train_type) or OPERATOR_COLOR_MAP.get(operator) or Color.orange()
    embed = discord.Embed(
        title=kwargs.get("title", "Train Info"),
        description=kwargs.get("message", "No message provided."),
        color=color
    )

    embed.add_field(name="Station", value=kwargs.get("station", "N/A"), inline=True)
    embed.add_field(name="Departure Time", value=kwargs.get("departure_time", "N/A"), inline=True)
    embed.add_field(name="Train Number", value=kwargs.get("train_number", "N/A"), inline=True)
    embed.add_field(name="Train Type", value=kwargs.get("train_type", "N/A"), inline=True)
    embed.add_field(name="Crowd Forecast", value=kwargs.get("crowd_info", "N/A"), inline=True)
    embed.add_field(name="Train Length", value=f"{kwargs.get('train_length', 0)} meters", inline=True)
    embed.add_field(name="Bakken (Train Cars)", value=str(kwargs.get("bakken_count", 0)), inline=True)
    seen = set()
    facilities_unique = [x for x in kwargs.get("facilities", []) if not (x in seen or seen.add(x))]
    embed.add_field(
        name="Facilities",
        value=", ".join(facilities_unique) if facilities_unique else "None",
        inline=True
    )

    embed.add_field(name="Operator", value="".join(kwargs.get("operator", "Unknown")) or "Unknown", inline=True)
    embed.add_field(name="Route Stations", value=", ".join(kwargs.get("route_stations", [])) or "None", inline=False)

    # Operator specific thumbnails (consider consolidating these or using a map for cleaner code)
    if operator == "NS":
        embed.set_thumbnail(url="https://substackcdn.com/image/fetch/f_auto,q_auto:good,fl_progressive:steep/https%3A%2F%2Fsubstack-post-media.s3.amazonaws.com%2Fpublic%2Fimages%2F02ef731e-d1f8-45d6-99d0-b4cdc1ce27c2_1200x1200.jpeg")
    elif operator == "Arriva":
        embed.set_thumbnail(url="https://cdn.brandfetch.io/arriva.nl/fallback/lettermark/theme/dark/h/256/w/256/icon?c=1bfwsmEH20zzEfSNTed")
    elif operator == "Breng":
        embed.set_thumbnail(url="https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQ5mpVyPKdD1-9-bktlimileVszZkIRHjhjeA&s")
    elif train_type == "EUROSTAR": # Using train_type here as per original code
        embed.set_thumbnail(url="https://play-lh.googleusercontent.com/8Wd7OAli64OdKnCvELCvfzJbSXxRfV_wmVpr4Gk8VPLpql1crDdJeDXULh3Fm5g8AQ")
    elif train_type == "ICE": # Using train_type here
        embed.set_thumbnail(url="https://marketingportal.extranet.deutschebahn.com/resource/blob/9692860/27bc0d931387a5806541b51d0eebd2d3/Bild_09-data.jpg")
    elif operator == "VIAS":
        embed.set_thumbnail(url="https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTU8wEsJ-bRz1_rgGcQWKRluzBgDn1NPl2jhw&s")
    # No thumbnail for THALYS yet as per original comment

    embed.set_footer(text="please donate! https://buymeacoffee.com/turret9", icon_url="https://cdn.discordapp.com/avatars/1357353682223497357/c9ec64ab5c138f915efeba9b96952d4d.webp?size=32")
    view = FetchTrainDataButtonDataResponder() # Re-instantiate view for each send
    embed.set_author(name=operator, icon_url=embed.thumbnail.url if embed.thumbnail else discord.Embed.Empty)

    if kwargs.get("image_byte_arr"):
        file = discord.File(fp=kwargs["image_byte_arr"], filename="train.png")
        embed.set_image(url="attachment://train.png")
        await channel.send(embed=embed, file=file) #, view=view)
    else:
        await channel.send(embed=embed)#, view=view)

def clean_announced_trains():
    global ANNOUNCED_TRAINS
    local_timezone = pytz.timezone("Europe/Amsterdam")
    now = datetime.now(local_timezone)

    to_remove = []

    for rit_id, info in ANNOUNCED_TRAINS.items():
        try:
            announcement_time = datetime.fromtimestamp(info["timestamp"], local_timezone)
            if now - announcement_time > timedelta(hours=2):
                to_remove.append(rit_id)
        except Exception as e:
            print(f"Error cleaning announced trains: {e}")
            to_remove.append(rit_id)

    for rit_id in to_remove:
        ANNOUNCED_TRAINS.pop(rit_id, None)

    if to_remove:
        print(f"Cleaned {len(to_remove)} old train announcements.")

# --- UI Views ---
class FetchTrainDataButtonDataResponder(discord.ui.View):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="more info", style=discord.ButtonStyle.primary)
    async def more_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        response_embed = discord.Embed(
            title="Button Clicked!",
            description=f"You clicked the '{button.label}' button.",
            color=discord.Color.blue()
        )
        response_embed.set_footer(text=f"Clicked by {interaction.user.display_name}")
        await interaction.response.send_message(embed=response_embed, ephemeral=True)

class DeparturesPaginator(ui.View):
    def __init__(self, station_code, departures, page_size=5):
        super().__init__(timeout=180)  # 3 minute timeout
        self.station_code = station_code
        self.departures = departures
        self.page_size = page_size
        self.current_page = 0
        self.total_pages = max(1, (len(departures) + page_size - 1) // page_size)

        self._update_buttons()

    def _update_buttons(self):
        self.previous_page.disabled = (self.current_page == 0)
        self.next_page.disabled = (self.current_page >= self.total_pages - 1)

    def get_current_page_embed(self):
        start_idx = self.current_page * self.page_size
        end_idx = min(start_idx + self.page_size, len(self.departures))
        current_departures = self.departures[start_idx:end_idx]

        local_timezone = pytz.timezone("Europe/Amsterdam")
        now = datetime.now(local_timezone)

        embed = discord.Embed(
            title=f"Upcoming Departures from {self.station_code}",
            description=f"Page {self.current_page + 1}/{self.total_pages}",
            color=discord.Color.blue()
        )

        for train in current_departures:
            departure_str = train.get('plannedDateTime')
            try:
                departure_time = datetime.strptime(departure_str, "%Y-%m-%dT%H:%M:%S%z")
                formatted_time = departure_time.strftime("%H:%M")
                time_diff = departure_time - now
                minutes_until = int(time_diff.total_seconds() / 60)
            except Exception:
                formatted_time = "Unknown"
                minutes_until = "?"

            train_number = train.get('product', {}).get('number', 'Unknown')
            train_type = train.get('product', {}).get('categoryCode', 'Unknown')
            direction = train.get('direction', 'Unknown')
            platform = train.get('plannedTrack', '?')

            delay_minutes = 0
            if train.get('actualDateTime'):
                try:
                    actual_time = datetime.strptime(train.get('actualDateTime'), "%Y-%m-%dT%H:%M:%S%z")
                    planned_time = datetime.strptime(departure_str, "%Y-%m-%dT%H:%M:%S%z")
                    delay_seconds = (actual_time - planned_time).total_seconds()
                    delay_minutes = int(delay_seconds / 60)
                except Exception:
                    delay_minutes = 0

            delay_text = f" (+{delay_minutes}m)" if delay_minutes > 0 else ""
            cancelled = "🚫 " if train.get('cancelled', False) else ""

            field_title = f"{formatted_time}{delay_text} • {train_type} {train_number} • Platform {platform}"
            field_value = f"{cancelled}To {direction} • Departs in {minutes_until} min"

            embed.add_field(name=field_title, value=field_value, inline=False)

        return embed

    @ui.button(label="◀️ Previous", style=ButtonStyle.gray)
    async def previous_page(self, interaction: discord.Interaction, button: ui.Button):
        self.current_page = max(0, self.current_page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_current_page_embed(), view=self)

    @ui.button(label="▶️ Next", style=ButtonStyle.gray)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        self.current_page = min(self.total_pages - 1, self.current_page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_current_page_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

# --- Autocomplete Functions ---
async def station_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    start_time = time.perf_counter()

    if not STATIONS_LIST:
        initialize_stations_cache()

    current_lower = current.lower()

    matches = word_prefix_map.get(current_lower, set())

    if matches:
        result = [
            app_commands.Choice(name=name, value=name)
            for name in sorted(list(matches))[:25] # Sort for consistent results
        ]
        end_time = time.perf_counter()
        print(f"Search: '{current}' | Time: {(end_time - start_time) * 1000:.2f}ms | Results: {len(result)} (prefix match)")
        return result

    found_matches = []
    for name in STATIONS_LIST:
        if current_lower in name.lower():
            found_matches.append(name)
            if len(found_matches) >= 25:
                break

    result = [
        app_commands.Choice(name=name, value=name)
        for name in found_matches
    ]
    end_time = time.perf_counter()
    print(f"Search: '{current}' | Time: {(end_time - start_time) * 1000:.2f}ms | Results: {len(result)} (substring search)")
    return result

async def train_type_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = current.lower()

    default_types = {"ICE", "VIRM", "DDZ", "IC", "SPR", "SLT", "SGM", "NS", "ICD", "FLIRT"}
    all_types = TRAIN_TYPES_CACHE.union(default_types)

    matches = [
        train_type for train_type in all_types
        if current in train_type.lower()
    ]

    matches = sorted(matches)

    return [
        app_commands.Choice(name=train_type, value=train_type)
        for train_type in matches[:25]
    ]

async def train_number_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = current.lower()

    matches = [
        train_num for train_num in TRAIN_NUMBERS_CACHE
        if current in train_num.lower()
    ]

    def sort_key(item):
        try:
            return int(item)
        except ValueError:
            return item

    matches = sorted(matches, key=sort_key)

    return [
        app_commands.Choice(name=train_num, value=train_num)
        for train_num in matches[:25]
    ]

# Get station code from name
def get_station_code(name):
    if not STATIONS_CACHE:
        initialize_stations_cache()

    return STATIONS_CACHE.get(name.lower())

class DepartureBoardImageGenerator:
    """
    A class to generate a departure board as an image.
    """
    def __init__(self):
        # Initialize scraper but do not call initialize_browser here
        self.classforscraping = NSStationInfoScraper()
        self.width = 800 # Default width, not directly used for screenshot clip
        self.height = 600 # Default height, not directly used for screenshot clip
        self.background_color = (20, 20, 20)
        self.text_color = (255, 255, 255)

    async def generate_board_image(self, station_name: str, departures: list, current_time: datetime) -> io.BytesIO | None:
        """
        Generates a departure board image, saves it to a temporary file,
        and then converts it into an in-memory bytestream.
        """
        # Define a temporary filename for the screenshot
        screenshot_filename = f"{station_name}_{current_time.strftime('%Y%m%d%H%M%S')}.png"

        # Generate the screenshot using your scraper class
        # Await the async function and check its success
        success = await self.classforscraping.get_station_departures_screenshot(station_name, screenshot_path=screenshot_filename)

        if not success:
            print(f"Screenshot generation failed for station {station_name}. Returning None.")
            return None

        # --- Convert the saved image to a BytesIO stream ---
        byte_stream = io.BytesIO()
        try:
            # Open the image using Pillow
            img = Image.open(screenshot_filename)
            # Save the image content to the BytesIO object as PNG
            img.save(byte_stream, format='PNG')
            # Seek to the beginning of the stream so it can be read
            byte_stream.seek(0)
            print(f"Image {screenshot_filename} loaded into bytestream.")
            return byte_stream
        except FileNotFoundError:
            print(f"Error: Screenshot file not found at {screenshot_filename} after generation attempt.")
            return None
        except Exception as e:
            print(f"An error occurred while processing the image into bytestream: {e}")
            return None
        finally:
            # Clean up the temporary screenshot file
            if os.path.exists(screenshot_filename):
                os.remove(screenshot_filename)
                print(f"Temporary screenshot {screenshot_filename} removed.")


image_generator = DepartureBoardImageGenerator()

# --- Looping Tasks ---
@tasks.loop(seconds=45)
async def departure_board_updater():
    """
    This loop updates the departure boards for channels that have an active board.
    It fetches data, generates an image board, and replaces the existing message.
    """
    print(f"Running departure board update loop. Active boards: {len(active_departure_boards)}")
    # Iterate over a copy of the dictionary to allow modification during iteration
    for channel_id, board_data in list(active_departure_boards.items()):
        channel = bot.get_channel(channel_id)
        if not channel:
            print(f"Channel {channel_id} not found, removing from active boards.")
            del active_departure_boards[channel_id]
            continue

        station_name = board_data['station']
        message_id = board_data.get('message_id')
        config = load_config()
        api_key = config.get("api_key")

        if not api_key:
            print(f"API key not configured for departure board update in channel {channel_id}.")
            continue

        url = "https://gateway.apiportal.ns.nl/reisinformatie-api/api/v2/departures"
        headers = {"Ocp-Apim-Subscription-Key": api_key}
        params = {"station": get_station_code(station_name).upper()} # Ensure station code is used

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status != 200:
                        print(f"Error fetching departures for {station_name}: {response.status}")
                        # Send an error message (text) if API fails
                        error_content = f"⚠️ Error fetching departures for {station_name}: API returned status {response.status}. Please try again later."
                        if message_id:
                            try:
                                old_message = await channel.fetch_message(message_id)
                                await old_message.delete() # If it was an image, delete and send new text error
                                new_message = await channel.send(error_content)
                                active_departure_boards[channel_id]['message_id'] = new_message.id
                            except discord.NotFound:
                                new_message = await channel.send(error_content)
                                active_departure_boards[channel_id]['message_id'] = new_message.id
                            except discord.Forbidden:
                                print(f"Missing permissions to edit/send error message in channel {channel.name}.")
                        else:
                            new_message = await channel.send(error_content)
                            active_departure_boards[channel_id]['message_id'] = new_message.id
                        continue # Skip to next channel

                    data = await response.json()

            departures = data.get("payload", {}).get("departures", [])
            local_timezone = pytz.timezone("Europe/Amsterdam")
            now = datetime.now(local_timezone)

            # --- AWAIT and check the image generation result ---
            image_bytes = await image_generator.generate_board_image(station_name, departures, now)

            if not image_bytes:
                print(f"Failed to generate departure board image for {station_name}. Sending text fallback.")
                error_content = f"❌ Could not generate departure board image for {station_name}. Please check bot logs for details."
                if message_id:
                    try:
                        old_message = await channel.fetch_message(message_id)
                        await old_message.delete()
                        new_message = await channel.send(error_content)
                        active_departure_boards[channel_id]['message_id'] = new_message.id
                    except discord.NotFound:
                        new_message = await channel.send(error_content)
                        active_departure_boards[channel_id]['message_id'] = new_message.id
                    except discord.Forbidden:
                        print(f"Missing permissions to edit/send error message in channel {channel.name}.")
                else:
                    new_message = await channel.send(error_content)
                    active_departure_boards[channel_id]['message_id'] = new_message.id
                continue # Skip to next channel

            discord_file = discord.File(image_bytes, filename=f"departure_board_{station_name.replace(' ', '_').lower()}.png")
            # --- End Image Generation ---

            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                    # To update an image, it's generally best practice to delete the old one
                    # and send a new one. Discord's `edit` method for attachments is complex.
                    await message.delete()
                    new_message = await channel.send(file=discord_file)
                    active_departure_boards[channel_id]['message_id'] = new_message.id
                    print(f"Updated departure board image in channel {channel.name} for {station_name}")
                except discord.NotFound:
                    print(f"Message {message_id} not found in channel {channel.name}, sending new image.")
                    new_message = await channel.send(file=discord_file)
                    active_departure_boards[channel_id]['message_id'] = new_message.id
                except discord.Forbidden:
                    print(f"Missing permissions to delete/send messages in channel {channel.name}. Removing from active boards.")
                    del active_departure_boards[channel_id] # Remove if bot can't manage messages
            else:
                # If no message_id is stored, send a new one
                try:
                    new_message = await channel.send(file=discord_file)
                    active_departure_boards[channel_id]['message_id'] = new_message.id
                    print(f"Sent initial departure board image in channel {channel.name} for {station_name}")
                except discord.Forbidden:
                    print(f"Missing permissions to send message in channel {channel.name}. Removing from active boards.")
                    del active_departure_boards[channel_id] # Remove if bot can't send

        except discord.Forbidden:
            print(f"Bot does not have permissions to send/delete messages in channel {channel.name} ({channel.id}). Removing from active boards.")
            del active_departure_boards[channel_id]
        except Exception as e:
            print(f"An error occurred while updating departure board in channel {channel.name} ({channel.id}): {e}")

@departure_board_updater.before_loop
async def before_departure_board_updater():
    await bot.wait_until_ready()
    print("Departure board updater is ready to start...")

@tasks.loop(seconds=15)
async def fetch_train_data():
    clean_announced_trains()

    config = load_config()
    if not config:
        return

    api_key = config.get("api_key")
    channel_configs = config.get("channels", {})

    if not channel_configs:
        print("No channel configurations found in config.")
        return

    url = "https://gateway.apiportal.ns.nl/reisinformatie-api/api/v2/departures"
    headers = {"Ocp-Apim-Subscription-Key": api_key}

    local_timezone = pytz.timezone("Europe/Amsterdam")
    now = datetime.now(local_timezone)

    async with aiohttp.ClientSession() as session:
        for channel_id, channel_config in channel_configs.items():
            channel = bot.get_channel(int(channel_id))
            if not channel:
                print(f"Couldn't find Discord channel with ID {channel_id}")
                continue

            stations = channel_config.get("stations", [])
            alerts = channel_config.get("alerts", [])
            train_type_notifications = channel_config.get("train_type_notifications", [])

            if not stations:
                print(f"No stations configured for channel {channel_id}")
                continue

            for station in stations:
                async with session.get(url, headers=headers, params={"station": station}) as response:
                    if response.status != 200:
                        print(f"Error getting data for station {station}: {response.status}")
                        continue

                    data = await response.json()

                    for train in data.get("payload", {}).get("departures", []):
                        train_number = train.get('product', {}).get('number')
                        departure_str = train.get('plannedDateTime')

                        if train_number:
                            TRAIN_NUMBERS_CACHE.add(str(train_number))

                        journey_id = train.get('journeyId', '')
                        if not journey_id:
                            category = train.get('product', {}).get('longCategoryName', '')
                            operator = train.get('product', {}).get('operatorName', '')
                            journey_id = f"{category}:{operator}:{train_number}:{departure_str}"

                        channel_journey_id = f"{channel_id}:{journey_id}"

                        try:
                            departure_time = datetime.strptime(departure_str, "%Y-%m-%dT%H:%M:%S%z")
                        except Exception as e:
                            print(f"Error parsing departure time: {e}")
                            continue

                        if now - departure_time > timedelta(minutes=0.5):
                            continue
                        if departure_time - now > timedelta(seconds=15):
                            continue

                        if channel_journey_id in ANNOUNCED_TRAINS:
                            continue

                        ANNOUNCED_TRAINS[channel_journey_id] = {
                            "timestamp": now.timestamp(),
                            "departure_time": departure_time.timestamp()
                        }
                        print(f"New train announcement for channel {channel_id}: {journey_id}")

                        info_url = f"https://gateway.apiportal.ns.nl/virtual-train-api/v1/trein/{train_number}"
                        async with session.get(info_url, headers=headers) as info_response:
                            if info_response.status == 200:
                                info = await info_response.json()
                                train_type = info.get("type", "Unknown")

                                if train_type and train_type != "Unknown":
                                    TRAIN_TYPES_CACHE.add(train_type)

                                crowd = info.get("drukteVoorspelling", {}).get("classification", "Unknown")
                                length = info.get("lengteInMeters", 0)
                                materieeldelen = info.get("materieeldelen", [])
                                images = [m.get("afbeelding") for m in materieeldelen if m.get("afbeelding")]
                                facilities = []
                                for m in materieeldelen:
                                    facilities += m.get("faciliteiten", [])
                                bakken_count = len(materieeldelen)
                            else:
                                print(f"Error getting train details: {info_response.status}")
                                train_type = "Unknown"
                                crowd = "Unknown"
                                length = 0
                                images = []
                                facilities = []
                                bakken_count = 1

                        route_stations = [r.get("mediumName", "Unknown") for r in train.get("routeStations", [])]
                        direction = train.get("direction", "Unknown")
                        operator = train.get('product', {}).get('operatorName', 'Unknown')

                        message = f"Train to {direction} from {station} has departed."

                        await send_discord_message_with_image(
                            channel,
                            message=message,
                            title=f"Departure from {station}",
                            station=station,
                            departure_time=departure_str,
                            train_number=train_number,
                            train_type=train_type,
                            crowd_info=crowd,
                            train_length=length,
                            facilities=facilities,
                            bakken_count=bakken_count,
                            route_stations=route_stations,
                            image_byte_arr=merge_images_horizontally(images) if images else None,
                            operator=operator
                        )

                        for notification in train_type_notifications:
                            if notification["train_type"].upper() == train_type.upper():
                                await channel.send(f"<@{notification['user_id']}> 🚨 **Train Alert:** Train `{train_number}` of type `{train_type}` is departing from `{station}` (to {direction}).")

                        for alert in alerts:
                            if alert["train_number"] == train_number and alert["station"].upper() == station.upper():
                                await channel.send(f"<@{alert['user_id']}> 🚨 **Train Alert:** Train `{train_number}` is departing from `{station}` (to {direction}).")

@tasks.loop(seconds=60)
async def change_presence():
    month = datetime.now().month
    base_activities = [
        (discord.Game(name="train simulator 5"), 1),
        (discord.Activity(type=discord.ActivityType.listening, name="train screeching noises"), 1),
        (discord.Activity(type=discord.ActivityType.watching, name="tracks being laid"), 1),
        (discord.Game(name="waiting for the next train"), 1),
        (discord.Activity(type=discord.ActivityType.listening, name="station announcements"), 1),
        (discord.Game(name="route optimization challenge"), 1),
        (discord.Activity(type=discord.ActivityType.watching, name="railway network maps"), 1),
        (discord.Game(name="on the rails"), 1),
    ]

    weighted_additional = [
        (discord.Streaming(name="turret9's donation pot", url="https://coff.ee/turret9"), 3),
        (discord.Streaming(name="railfan livestream", url="https://coff.ee/turret9"), 2),
    ]

    seasonal_activities = []
    if month in [12, 1, 2]:
        seasonal_activities = [
            (discord.Game(name="shoveling snow off the tracks"), 1),
            (discord.Activity(type=discord.ActivityType.listening, name="icy rail chatter"), 1),
        ]
    elif month in [6, 7, 8]:
        seasonal_activities = [
            (discord.Game(name="melting in the signal box"), 1),
            (discord.Activity(type=discord.ActivityType.watching, name="heat-distorted rail lines"), 1),
        ]

    all_activities = base_activities + weighted_additional + seasonal_activities
    population, weights = zip(*all_activities)

    activity = random.choices(population=population, weights=weights, k=1)[0]

    statuses = [discord.Status.online, discord.Status.idle, discord.Status.dnd]
    status = random.choice(statuses)

    await bot.change_presence(activity=activity, status=status)

# --- Discord Commands ---
@bot.tree.command(name="addstation", description="Add a station to monitor in this channel")
@app_commands.describe(station="The station name to monitor (e.g., Utrecht Centraal)")
@app_commands.autocomplete(station=station_autocomplete)
async def addstation(interaction: discord.Interaction, station: str):
    """Add a station to the monitoring list for this specific channel"""
    station = get_station_code(station)
    print(station)
    config = load_config()
    if not config:
        await interaction.response.send_message("❌ Config file not found!", ephemeral=True)
        return

    channel_id = str(interaction.channel.id)

    if "channels" not in config:
        config["channels"] = {}

    if channel_id not in config["channels"]:
        config["channels"][channel_id] = {
            "stations": [],
            "alerts": [],
            "train_type_notifications": []
        }

    if station in config["channels"][channel_id]["stations"]:
        await interaction.response.send_message(f"❌ Station `{station}` is already being monitored in this channel!", ephemeral=True)
        return

    config["channels"][channel_id]["stations"].append(station)

    try:
        with open("config.json", "w") as f:
            json.dump(config, f, indent=2)

        await interaction.response.send_message(f"✅ Added `{station}` to monitoring list for this channel!")
        print(f"Added station '{station}' to channel {channel_id}")

    except Exception as e:
        await interaction.response.send_message(f"❌ Error saving config: {e}", ephemeral=True)
        print(f"Error saving config: {e}")

@bot.tree.command(name="removestation", description="Remove a station from monitoring in this channel")
async def removestation(interaction: discord.Interaction, station: str):
    """Remove a station from the monitoring list for this specific channel"""

    config = load_config()
    if not config:
        await interaction.response.send_message("❌ Config file not found!", ephemeral=True)
        return

    channel_id = str(interaction.channel.id)

    if "channels" not in config or channel_id not in config["channels"]:
        await interaction.response.send_message("❌ This channel has no stations configured!", ephemeral=True)
        return

    if station not in config["channels"][channel_id]["stations"]:
        await interaction.response.send_message(f"❌ Station `{station}` is not being monitored in this channel!", ephemeral=True)
        return

    config["channels"][channel_id]["stations"].remove(station)

    if not config["channels"][channel_id]["stations"] and not config["channels"][channel_id]["alerts"] and not config["channels"][channel_id]["train_type_notifications"]:
        del config["channels"][channel_id]

    try:
        with open("config.json", "w") as f:
            json.dump(config, f, indent=2)

        await interaction.response.send_message(f"✅ Removed `{station}` from monitoring list for this channel!")
        print(f"Removed station '{station}' from channel {channel_id}")

    except Exception as e:
        await interaction.response.send_message(f"❌ Error saving config: {e}", ephemeral=True)
        print(f"Error saving config: {e}")

@bot.tree.command(name="liststations", description="List all stations being monitored in this channel")
async def liststations(interaction: discord.Interaction):
    """List all stations being monitored in this specific channel"""

    config = load_config()
    if not config:
        await interaction.response.send_message("❌ Config file not found!", ephemeral=True)
        return

    channel_id = str(interaction.channel.id)

    if "channels" not in config or channel_id not in config["channels"] or not config["channels"][channel_id]["stations"]:
        await interaction.response.send_message("❌ No stations are being monitored in this channel!", ephemeral=True)
        return

    stations = config["channels"][channel_id]["stations"]
    station_list = "\n".join([f"• {station}" for station in stations])

    embed = discord.Embed(
        title="🚂 Monitored Stations in This Channel",
        description=station_list,
        color=0x0099ff
    )

    await interaction.response.send_message(embed=embed)

@tree.command(name="apistatus", description="Check the status of the NS API")
async def checkapistatus(interaction: discord.Interaction):

    config = load_config()

    await interaction.response.defer()

    endpoints = {
        "Gateway Ping": {
            "url": "https://gateway.apiportal.ns.nl/",
            "expected_status": 404,
            "headers": {}
        },
        "Virtual Train API": {
            "url": "https://gateway.apiportal.ns.nl/virtual-train-api/v1/trein",
            "expected_status": 200,
            "headers": {
                "Ocp-Apim-Subscription-Key": config.get("api_key")
            }
        },
        "Departure API": {
            "url": "https://gateway.apiportal.ns.nl/reisinformatie-api/api/v2/departures?station=WW",
            "expected_status": 200,
            "headers": {
                "Ocp-Apim-Subscription-Key": config.get("api_key")
            }
        }
    }

    embed = discord.Embed(
        title="NS API Backend Check",
        description="Checking the status of NS API endpoints...",
        color=0xffc61e
    )
    embed.set_thumbnail(url="https://substackcdn.com/image/fetch/f_auto,q_auto:good,fl_progressive:steep/https%3A%2F%2Fsubstack-post-media.s3.amazonaws.com%2Fpublic%2Fimages%2F02ef731e-d1f8-45d6-99d0-b4cdc1ce27c2_1200x1200.jpeg")

    async with aiohttp.ClientSession() as session:
        for name, data in endpoints.items():
            start = time.monotonic()
            try:
                headers = data.get("headers", {})
                async with session.get(data["url"], headers=headers, timeout=5) as response:
                    duration = time.monotonic() - start
                    if response.status == data["expected_status"]:
                        if duration * 1000 < 1000:
                            embed.add_field(
                                name=name,
                                value=f"✅ **Expected** ({int(duration * 1000)} ms)",
                                inline=False
                            )
                        else:
                            embed.add_field(
                                name=name,
                                value=f"⚠️ **slow** ({int(duration * 1000)} ms)", # Added 'ms' for clarity
                                inline=False # Consistent inline
                            )
                    else:
                        embed.add_field(
                            name=name,
                            value=f"❌ **failed** {response.status} (Expected: {data['expected_status']})",
                            inline=False
                        )
            except Exception as e:
                embed.add_field(
                    name=name,
                    value=f"❌ Error: `{type(e).__name__}` - {str(e)[:100]}",
                    inline=False
                )

    await interaction.followup.send(embed=embed)

@tree.command(name="routeinfo", description="Get live info about a specific train number (ritnummer).")
@app_commands.describe(train_number="The train number (ritnummer) to look up.")
@app_commands.autocomplete(train_number=train_number_autocomplete)
async def route_info(interaction: discord.Interaction, train_number: str):
    await interaction.response.defer()

    config = load_config()
    api_key = config.get("api_key")
    if not api_key:
        await interaction.followup.send("API key not configured.")
        return

    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Accept": "application/json"
    }

    url = f"https://gateway.apiportal.ns.nl/virtual-train-api/v1/trein/{train_number}"
    # Use aiohttp for consistency in async context
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                await interaction.followup.send(f"❌ Could not fetch info for train `{train_number}`. Status: {response.status}")
                return
            data = await response.json()

    train_type = data.get("type", "Unknown")

    if train_type and train_type != "Unknown":
        TRAIN_TYPES_CACHE.add(train_type)

    crowd_info = data.get("drukteVoorspelling", {}).get("classification", "Unknown")
    train_length = data.get("lengteInMeters", 0)
    materieeldelen = data.get("materieeldelen", [])
    bakken_count = len(materieeldelen)

    train_images = [m.get("afbeelding") for m in materieeldelen if m.get("afbeelding")]
    facilities = []
    for m in materieeldelen:
        facilities += m.get("faciliteiten", [])

    route_stations = data.get("bestemmingStations", [])
    # route_names = [st.get("mediumName", "Unknown") for st in route_stations] # Not used

    embed = discord.Embed(
        title=f"Train {train_number} Info",
        description=f"Live data for route",
        color=discord.Color.blue()
    )
    embed.add_field(name="Train Type", value=train_type, inline=True)
    embed.add_field(name="Crowd Forecast", value=crowd_info, inline=True)
    embed.add_field(name="Train Length", value=f"{train_length} meters", inline=True)
    embed.add_field(name="Bakken (Train Cars)", value=str(bakken_count), inline=True)
    embed.add_field(name="Facilities", value=", ".join(facilities) if facilities else "None", inline=False)

    files = None
    if train_images:
        # merge_images_horizontally uses synchronous requests, wrap it in run_in_executor
        image_bytes = await bot.loop.run_in_executor(None, merge_images_horizontally, train_images)
        file = discord.File(fp=image_bytes, filename="route.png")
        embed.set_image(url="attachment://route.png")
        files = [file]

    await interaction.followup.send(embed=embed, files=files if files else None)

@tree.command(name="settrainalert", description="Set an alert for a specific train.")
@app_commands.describe(
    train_number="The train number (ritnummer) to alert for.",
    station_name="The station name to monitor"
)
@app_commands.autocomplete(
    train_number=train_number_autocomplete,
    station_name=station_autocomplete
)
async def set_train_alert(interaction: discord.Interaction, train_number: str, station_name: str):
    station_code = get_station_code(station_name)
    if not station_code:
        await interaction.response.send_message(f"❌ Could not find station code for '{station_name}'.", ephemeral=True)
        return

    config = load_config()
    # Ensure alerts are stored per channel
    channel_id = str(interaction.channel.id)
    if "channels" not in config:
        config["channels"] = {}
    if channel_id not in config["channels"]:
        config["channels"][channel_id] = {"stations": [], "alerts": [], "train_type_notifications": []}
    alerts = config["channels"][channel_id].get("alerts", [])

    for alert in alerts:
        if alert["train_number"] == train_number and alert["station"].upper() == station_code.upper():
            await interaction.response.send_message(f"🚨 You are already subscribed to alerts for train `{train_number}` at station `{station_name}`.", ephemeral=True)
            return

    alert = {
        "train_number": train_number,
        "station": station_code.upper(),
        "user_id": interaction.user.id
    }

    alerts.append(alert)
    config["channels"][channel_id]["alerts"] = alerts # Update alerts for this specific channel
    save_config(config)

    await interaction.response.send_message(f"✅ Alert set for train `{train_number}` at station `{station_name}` ({station_code.upper()}). You'll be notified when it departs.", ephemeral=True)

@tree.command(name="listalerts", description="List all your active train alerts.")
async def list_train_alerts(interaction: discord.Interaction):
    config = load_config()
    user_id = interaction.user.id
    channel_id = str(interaction.channel.id)

    alerts = config.get("channels", {}).get(channel_id, {}).get("alerts", [])

    user_alerts = [a for a in alerts if a.get("user_id") == user_id]
    if not user_alerts:
        await interaction.response.send_message("🚫 You have no active train alerts in this channel.", ephemeral=True)
        return

    alert_list = "\n".join([f"Train `{a['train_number']}` at station `{a['station']}`" for a in user_alerts])
    await interaction.response.send_message(f"📣 **Your Train Alerts in this channel:**\n{alert_list}", ephemeral=True)

@tree.command(name="removealert", description="Remove an alert for a specific train.")
@app_commands.describe(
    train_number="The train number (ritnummer) to remove.",
    station_name="The station name to remove the alert for"
)
@app_commands.autocomplete(
    train_number=train_number_autocomplete,
    station_name=station_autocomplete
)
async def remove_train_alert(interaction: discord.Interaction, train_number: str, station_name: str):
    station_code = get_station_code(station_name)
    if not station_code:
        await interaction.response.send_message(f"❌ Could not find station code for '{station_name}'.", ephemeral=True)
        return

    config = load_config()
    channel_id = str(interaction.channel.id)
    if "channels" not in config or channel_id not in config["channels"]:
        await interaction.response.send_message("❌ This channel has no alerts configured!", ephemeral=True)
        return

    alerts = config["channels"][channel_id].get("alerts", [])

    original_count = len(alerts)
    alerts = [alert for alert in alerts if not (
        alert["train_number"] == train_number and alert["station"] == station_code.upper()
    )]

    if len(alerts) == original_count:
        await interaction.response.send_message(
            f"⚠️ No alert found for train `{train_number}` at station `{station_name}` ({station_code.upper()}) in this channel.",
            ephemeral=True
        )
        return

    config["channels"][channel_id]["alerts"] = alerts
    save_config(config)

    await interaction.response.send_message(
        f"✅ Alert for train `{train_number}` at station `{station_name}` ({station_code.upper()}) removed from this channel.",
        ephemeral=True
    )

@tree.command(name="settrainnotificationtype", description="Set a notification for a specific train type (e.g., ICE, VIRM, DDZ).")
@app_commands.describe(train_type="The train type to notify for (e.g., ICE, VIRM, DDZ).")
@app_commands.autocomplete(train_type=train_type_autocomplete)
async def set_train_notification_type(interaction: discord.Interaction, train_type: str):
    config = load_config()
    channel_id = str(interaction.channel.id)

    if "channels" not in config:
        config["channels"] = {}
    if channel_id not in config["channels"]:
        config["channels"][channel_id] = {"stations": [], "alerts": [], "train_type_notifications": []}

    train_type_notifications = config["channels"][channel_id].get("train_type_notifications", [])

    for notification in train_type_notifications:
        if notification["train_type"].upper() == train_type.upper() and notification["user_id"] == interaction.user.id:
            await interaction.response.send_message(f"🚨 You are already subscribed to notifications for train type `{train_type}` in this channel.", ephemeral=True)
            return

    notification = {
        "train_type": train_type.upper(),
        "user_id": interaction.user.id
    }

    train_type_notifications.append(notification)
    config["channels"][channel_id]["train_type_notifications"] = train_type_notifications
    save_config(config)

    await interaction.response.send_message(f"✅ Notification set for train type `{train_type.upper()}` in this channel. You'll be notified when a train of this type departs.", ephemeral=True)

@tree.command(name="listtrainnotifications", description="List all your active train type notifications.")
async def list_train_type_notifications(interaction: discord.Interaction):
    config = load_config()
    user_id = interaction.user.id
    channel_id = str(interaction.channel.id)

    train_type_notifications = config.get("channels", {}).get(channel_id, {}).get("train_type_notifications", [])

    user_notifications = [n for n in train_type_notifications if n["user_id"] == user_id]

    if not user_notifications:
        await interaction.response.send_message("🚫 You have no active train type notifications in this channel.", ephemeral=True)
        return

    notification_list = "\n".join([f"Train type `{n['train_type']}`" for n in user_notifications])
    await interaction.response.send_message(f"📣 **Your Train Type Notifications in this channel:**\n{notification_list}", ephemeral=True)

@tree.command(name="removetrainnotification", description="Remove an active notification for a specific train type.")
@app_commands.describe(train_type="The train type (e.g., ICE, VIRM, DDZ) to remove the notification for.")
@app_commands.autocomplete(train_type=train_type_autocomplete)
async def remove_train_notification(interaction: discord.Interaction, train_type: str):
    config = load_config()
    channel_id = str(interaction.channel.id)
    if "channels" not in config or channel_id not in config["channels"]:
        await interaction.response.send_message("❌ This channel has no train type notifications configured!", ephemeral=True)
        return

    train_type_notifications = config["channels"][channel_id].get("train_type_notifications", [])

    original_count = len(train_type_notifications)
    train_type_notifications = [notification for notification in train_type_notifications
                                if not (notification["train_type"].upper() == train_type.upper() and notification["user_id"] == interaction.user.id)]

    if len(train_type_notifications) == original_count:
        await interaction.response.send_message(f"⚠️ No notification found for train type `{train_type}` in this channel.", ephemeral=True)
        return

    config["channels"][channel_id]["train_type_notifications"] = train_type_notifications
    save_config(config)

    await interaction.response.send_message(f"✅ Notification for train type `{train_type}` removed from this channel.", ephemeral=True)

@tree.command(name="listdepartures", description="List upcoming departures from a specific station.")
@app_commands.describe(station_name="The station name to check departures for")
@app_commands.autocomplete(station_name=station_autocomplete)
async def list_departures(interaction: discord.Interaction, station_name: str):
    await interaction.response.defer()

    station_code = get_station_code(station_name)
    if not station_code:
        await interaction.followup.send(f"❌ Could not find station code for '{station_name}'.", ephemeral=True)
        return

    config = load_config()
    api_key = config.get("api_key")
    if not api_key:
        await interaction.followup.send("API key not configured.")
        return

    url = "https://gateway.apiportal.ns.nl/reisinformatie-api/api/v2/departures"
    headers = {"Ocp-Apim-Subscription-Key": api_key}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params={"station": station_code.upper()}) as response:
            if response.status != 200:
                await interaction.followup.send(f"❌ Could not fetch departures for station `{station_name}` ({station_code.upper()}). Error: {response.status}")
                return
            data = await response.json()

    departures = data.get("payload", {}).get("departures", [])

    if not departures:
        await interaction.followup.send(f"No upcoming departures found for station `{station_code.upper()}`.")
        return

    paginator = DeparturesPaginator(station_code.upper(), departures)

    await interaction.followup.send(embed=paginator.get_current_page_embed(), view=paginator)

@tree.command(name="planroute", description="Plan a route between two stations and DM you the results.")
@app_commands.describe(
    from_station="Origin station name",
    to_station="Destination station name",
    departure_time="(Optional) Departure time in RFC3339 format (e.g., 2025-04-05T15:30:00+02:00). Defaults to now."
)
@app_commands.autocomplete(
    from_station=station_autocomplete,
    to_station=station_autocomplete
)
async def plan_route(interaction: discord.Interaction, from_station: str, to_station: str, departure_time: str = None):
    await interaction.response.defer(ephemeral=True)

    from_station_code = get_station_code(from_station)
    to_station_code = get_station_code(to_station)

    if not from_station_code:
        await interaction.followup.send(f"Could not find station code for origin '{from_station}'.", ephemeral=True)
        return
    if not to_station_code:
        await interaction.followup.send(f"Could not find station code for destination '{to_station}'.", ephemeral=True)
        return

    config = load_config()
    api_key = config.get("api_key")
    if not api_key:
        await interaction.followup.send("API key not configured.", ephemeral=True)
        return

    local_timezone = pytz.timezone("Europe/Amsterdam")

    if departure_time:
        try:
            parsed_dt = datetime.fromisoformat(departure_time)
            departure_time = parsed_dt.isoformat()
        except ValueError:
            await interaction.followup.send("Invalid departure_time format. Please use RFC3339 format.", ephemeral=True)
            return
    else:
        departure_time = datetime.now(local_timezone).isoformat()

    url = "https://gateway.apiportal.ns.nl/reisinformatie-api/api/v3/trips"
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    params = {
        "fromStation": from_station_code.upper(),
        "toStation": to_station_code.upper(),
        "dateTime": departure_time,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status != 200:
                    await interaction.followup.send(f"NS API returned an error: {response.status}", ephemeral=True)
                    return
                data = await response.json()
    except Exception as e:
        await interaction.followup.send(f"Error contacting the NS API: {e}", ephemeral=True)
        return

    trips = data.get("trips", []) if isinstance(data, dict) else data[0].get("trips", []) if isinstance(data, list) and data else []

    if not trips:
        await interaction.followup.send("No trips available for this route.", ephemeral=True)
        return

    def format_time(time_str: str) -> str:
        try:
            dt = datetime.fromisoformat(time_str)
            dt_local = dt.astimezone(local_timezone)
            return dt_local.strftime("%H:%M")
        except Exception:
            return time_str

    trip = trips[0]

    duration = trip.get("plannedDurationInMinutes", "Unknown")
    transfers = trip.get("transfers", "Unknown")
    status = trip.get("status", "Unknown")

    embeds = []

    header_embed = discord.Embed(
        title="Travel Route Summary",
        description=(
            f"From: {from_station}\n"
            f"To: {to_station}\n"
            f"Departure Time: {format_time(departure_time)}\n"
        ),
        color=discord.Color.dark_blue()
    )
    embeds.append(header_embed)

    for notice in trip.get("travelAssistanceInfo", {}).get("notices", []):
        embeds.append(discord.Embed(
            title="Notice",
            description=notice.get("text", "No details"),
            color=discord.Color.orange()
        ))

    for leg_idx, leg in enumerate(trip.get("legs", []), start=1):
        origin = leg.get("origin", {})
        destination = leg.get("destination", {})

        dep_time = format_time(origin.get("plannedDateTime", "Unknown"))
        arr_time = format_time(destination.get("plannedDateTime", "Unknown"))

        dep_track = origin.get("plannedTrack", "N/A")
        arr_track = destination.get("plannedTrack", "N/A")

        train_info = ""
        product = leg.get("product", {})
        if product:
            train_type = product.get("categoryCode", "N/A")
            train_number = product.get("number", "N/A")
            TRAIN_TYPES_CACHE.add(train_type)
            TRAIN_NUMBERS_CACHE.add(str(train_number))
            train_info = f"{train_type} {train_number}"

        leg_embed = discord.Embed(
            title=f"Leg {leg_idx}: {dep_time}: {origin.get('name', 'unknown')}({dep_track}) -> {destination.get('name', 'Unknown')}({arr_track}). {arr_time} ",
            description=None,
            color=discord.Color.green()
        )
        embeds.append(leg_embed)

    trip_footer = discord.Embed(
        title="Summary",
        description=f"Total Duration: {duration} min\nTransfers: {transfers}\nStatus: {status}",
        color=discord.Color.dark_green()
    )
    embeds.append(trip_footer)

    try:
        for i in range(0, len(embeds), 10):
            await interaction.user.send(embeds=embeds[i:i+10])
        await interaction.followup.send("I've sent you the route details via DM.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Could not DM you the route details. Please check your DM settings. Error: {e}", ephemeral=True)

@bot.tree.command(name="setup", description="Adds this channel to the config as a target for train messages.")
async def setup(interaction: discord.Interaction):
    """Adds the current channel to the list of target channels in the config."""
    config = load_config()

    if config is None:
        await interaction.response.send_message("There was an issue loading the configuration file.", ephemeral=True)
        return

    channel_id = str(interaction.channel.id)

    # The 'discord_channel_ids' is for old config structure or general alerts.
    # We now primarily use the 'channels' object for per-channel configuration.
    # This command should probably be updated to configure per-channel aspects.
    # For now, keeping original logic for 'discord_channel_ids' for compatibility.
    if channel_id in config.get("discord_channel_ids", []):
        await interaction.response.send_message(f"Channel {interaction.channel.name} is already in the list.", ephemeral=True)
        return

    config.setdefault("discord_channel_ids", []).append(channel_id)
    save_config(config)

    await interaction.response.send_message(f"Channel {interaction.channel.name} has been added to the list of target channels.", ephemeral=True)

@bot.tree.command(name="remove", description="Removes this channel from the config's list of target channels.")
async def remove_channel(interaction: discord.Interaction):
    """Removes the current channel from the list of target channels in the config."""
    config = load_config()

    if config is None:
        await interaction.response.send_message("There was an issue loading the configuration file.", ephemeral=True)
        return

    channel_id = str(interaction.channel.id)

    if channel_id not in config.get("discord_channel_ids", []):
        await interaction.response.send_message(f"Channel {interaction.channel.name} is not in the target channel list.", ephemeral=True)
        return

    config["discord_channel_ids"].remove(channel_id)
    save_config(config)

    await interaction.response.send_message(f"Channel {interaction.channel.name} has been removed from the target channel list.", ephemeral=True)

# --- NEW: Departure Board Commands ---
@bot.tree.command(name="departure-board", description="Send an updating departure board. Recommended to clear the channel first.")
@app_commands.autocomplete(station=station_autocomplete)
async def departureboardimg(interaction: discord.Interaction, station: str):
    """
    
    Sets up an updating departure board in the current channel for the specified station.
    It will send an initial message and then continuously update it.
    """
    station = get_station_code(station).lower()
    channel_id = interaction.channel_id

    # Validate station exists
    station_code = get_station_code(station)
    if not station_code:
        await interaction.response.send_message(f"❌ Station '{station}' not found. Please select from autocomplete suggestions.", ephemeral=True)
        return

    if channel_id in active_departure_boards:
        # Board already active, update the station and reset message_id to trigger a new message/edit
        active_departure_boards[channel_id]['station'] = station
        active_departure_boards[channel_id]['message_id'] = None # Force a new message or re-find and edit
        await interaction.response.send_message(f"Updating existing departure board in this channel to **{station}**. It will refresh shortly.", ephemeral=True)
    else:
        # No board active, start a new one
        active_departure_boards[channel_id] = {'station': station, 'message_id': None}
        await interaction.response.send_message(f"Starting an updating departure board for **{station}** in this channel. Please wait for the first update...", ephemeral=True)

    # Ensure the updater loop is running. It's started in on_ready, but this is a safeguard.
    if not departure_board_updater.is_running():
        departure_board_updater.start()

@bot.tree.command(name="stop-departure-board", description="Stop the updating departure board in this channel.")
async def stop_departure_board(interaction: discord.Interaction):
    """
    Stops the continuous updating of the departure board in the current channel.
    """
    channel_id = interaction.channel_id

    if channel_id in active_departure_boards:
        # Optionally, try to delete the last sent message
        message_id_to_delete = active_departure_boards[channel_id].get('message_id')
        del active_departure_boards[channel_id]
        if message_id_to_delete:
            try:
                message = await interaction.channel.fetch_message(message_id_to_delete)
                await message.delete()
                await interaction.response.send_message("✅ Stopped the updating departure board and removed its last message in this channel.", ephemeral=True)
            except discord.NotFound:
                await interaction.response.send_message("✅ Stopped the updating departure board in this channel (message already removed).", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message("✅ Stopped the updating departure board, but couldn't delete the message (missing permissions).", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"✅ Stopped the updating departure board, but an error occurred trying to delete the message: {e}", ephemeral=True)
        else:
            await interaction.response.send_message("✅ Stopped the updating departure board in this channel.", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ No active departure board found in this channel to stop.", ephemeral=True)

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print("🌐 Syncing slash commands...")

    try:
        print("Initializing stations cache...")
        initialize_stations_cache()
        print("Stations cache initialized!")

        synced = await tree.sync()
        print(f"🔧 Synced {len(synced)} command(s).\n")

        print("Initializing Playwright browser for image generation...")
        await image_generator.classforscraping.initialize_browser()
        print("Playwright browser initialized!\n")

        # Start all tasks
        change_presence.start()
        fetch_train_data.start()
        departure_board_updater.start() # Start the new departure board updater task
    except Exception as e:
        print(f"❌ Error syncing commands or starting tasks: {e}")

# --- Bot Run ---
try:
    DISCORD_BOT_TOKEN = load_config().get("discord_bot_token")
    if not DISCORD_BOT_TOKEN:
        print("Error: 'discord_bot_token' not found in config.json. Please add it.")
        exit(1)
    bot.run(DISCORD_BOT_TOKEN)
except Exception as e:
    print(f"Error running the bot: {e}")
    print("Exiting...")
    exit(0)
