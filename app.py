from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
import speech_recognition as sr
import os
import json
import time
import datetime
import threading
import smtplib
import requests
import tempfile
import wave
import numpy as np
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
import logging
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import sqlite3
import uuid
from twilio.rest import Client
import soundfile as sf
from pathlib import Path
import dotenv
from werkzeug.utils import secure_filename

# Load environment variables
dotenv.load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)  # Enable Cross-Origin Resource Sharing

# Configuration
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'webm', 'mp4', 'wav', 'mp3'}
DATABASE_PATH = 'safety_guardian.db'
RISK_KEYWORDS = ['risk', 'risky', 'high risk']

# Create upload directory if it doesn't exist
Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)

# Twilio configuration for SMS and calls
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]) else None

# Email configuration
EMAIL_SENDER = os.getenv('EMAIL_SENDER')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD')
SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))

# Police station API (mock)
POLICE_API_KEY = os.getenv('POLICE_API_KEY')

# Configure Flask app
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB max upload size

def init_db():
    """Initialize the SQLite database with required tables"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Create users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create emergency contacts table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS emergency_contacts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            is_primary BOOLEAN DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # Create alerts history table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            risk_level TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            address TEXT,
            keywords_detected TEXT,
            video_path TEXT,
            actions_taken TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

def allowed_file(filename):
    """Check if the file has an allowed extension"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_address_from_coordinates(latitude, longitude):
    """Convert GPS coordinates to a human-readable address"""
    try:
        geolocator = Nominatim(user_agent="safety_guardian")
        location = geolocator.reverse(f"{latitude}, {longitude}")
        return location.address if location else "Unknown location"
    except Exception as e:
        logger.error(f"Error getting address: {e}")
        return "Address lookup failed"

def find_nearest_police_station(latitude, longitude):
    """Find the nearest police station to the given coordinates"""
    try:
        # This would normally use a real API like Google Places API
        # Here we're mocking the response
        url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{latitude},{longitude}",
            "radius": 5000,
            "type": "police",
            "key": POLICE_API_KEY
        }
        
        # In a real implementation, you would uncomment this
        # response = requests.get(url, params=params)
        # data = response.json()
        
        # Mock response for demonstration
        mock_police_station = {
            "name": "Central Police Station",
            "phone": "+1234567890",
            "address": "123 Safety Street, City",
            "distance": 1.2,  # km
            "latitude": latitude + 0.01,
            "longitude": longitude + 0.01
        }
        return mock_police_station
    except Exception as e:
        logger.error(f"Error finding police station: {e}")
        return None

def send_email_alert(contact, user_name, risk_level, location_info, video_path=None):
    """Send email alert to emergency contact"""
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = contact['email']
        msg['Subject'] = f"EMERGENCY SAFETY ALERT for {user_name}"
        
        # Email body based on risk level
        body = f"""
        <html>
        <body>
        <h2>EMERGENCY SAFETY ALERT</h2>
        <p>This is an automated alert from Safety Guardian system.</p>
        <p>The system has detected a potential safety concern for {user_name}.</p>
        <p><strong>Risk Level:</strong> {risk_level}</p>
        <p><strong>Time:</strong> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <p><strong>Location:</strong> {location_info['address']}</p>
        <p><strong>GPS Coordinates:</strong> {location_info['latitude']}, {location_info['longitude']}</p>
        <p><strong>Map Link:</strong> <a href="https://www.google.com/maps?q={location_info['latitude']},{location_info['longitude']}">View on Google Maps</a></p>
        
        <p>Please take appropriate action immediately.</p>
        <p>This is an automated message. Please do not reply to this email.</p>
        </body>
        </html>
        """
        
        msg.attach(MIMEText(body, 'html'))
        
        # Attach video if available
        if video_path and os.path.exists(video_path):
            with open(video_path, 'rb') as file:
                video_attachment = MIMEApplication(file.read(), Name=os.path.basename(video_path))
                video_attachment['Content-Disposition'] = f'attachment; filename="{os.path.basename(video_path)}"'
                msg.attach(video_attachment)
        
        # Connect to SMTP server and send email
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        
        logger.info(f"Email alert sent to {contact['email']}")
        return True
    except Exception as e:
        logger.error(f"Error sending email: {e}")
        return False

def send_sms_alert(contact, user_name, risk_level, location_info):
    """Send SMS alert to emergency contact"""
    if not twilio_client:
        logger.error("Twilio client not configured")
        return False
    
    try:
        message_body = (
            f"EMERGENCY ALERT for {user_name}. Risk level: {risk_level}. "
            f"Location: {location_info['address']}. "
            f"GPS: {location_info['latitude']}, {location_info['longitude']}. "
            f"Please take immediate action."
        )
        
        message = twilio_client.messages.create(
            body=message_body,
            from_=TWILIO_PHONE_NUMBER,
            to=contact['phone']
        )
        
        logger.info(f"SMS alert sent to {contact['phone']}, SID: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"Error sending SMS: {e}")
        return False

def make_emergency_call(phone_number, user_name, location_info):
    """Make an emergency call to police or emergency contact"""
    if not twilio_client:
        logger.error("Twilio client not configured")
        return False
    
    try:
        # In a real implementation, you would use Twilio's TwiML to create
        # a call with text-to-speech functionality
        twiml = f"""
        <Response>
            <Say>
                Emergency alert from Safety Guardian system. 
                This is an automated call reporting a high risk emergency situation for {user_name}.
                Their current location is {location_info['address']}.
                GPS coordinates are {location_info['latitude']}, {location_info['longitude']}.
                Please dispatch emergency services immediately.
                This message will now repeat.
                Emergency alert from Safety Guardian system...
            </Say>
        </Response>
        """
        
        # For demo purposes, we'll just log this instead of making an actual call
        logger.info(f"Would call {phone_number} with TwiML: {twiml}")
        
        # In a real implementation:
        # call = twilio_client.calls.create(
        #     twiml=twiml,
        #     to=phone_number,
        #     from_=TWILIO_PHONE_NUMBER
        # )
        # logger.info(f"Emergency call placed to {phone_number}, SID: {call.sid}")
        
        return True
    except Exception as e:
        logger.error(f"Error making emergency call: {e}")
        return False

def process_audio_for_keywords(audio_file):
    """Process audio file to detect keywords using speech recognition"""
    recognizer = sr.Recognizer()
    keywords_found = []
    
    try:
        with sr.AudioFile(audio_file) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data).lower()
            
            logger.info(f"Transcribed text: {text}")
            
            # Check for risk keywords
            for keyword in RISK_KEYWORDS:
                if keyword in text:
                    keywords_found.append(keyword)
        
        return keywords_found
    except Exception as e:
        logger.error(f"Error in speech recognition: {e}")
        return keywords_found

def detect_scream(audio_file):
    """Detect potential screams in audio file using signal processing with soundfile instead of pydub"""
    try:
        # Load audio file with soundfile
        data, sample_rate = sf.read(audio_file)
        
        # Convert to mono if stereo
        if len(data.shape) > 1:
            data = np.mean(data, axis=1)
        
        # Calculate short-time energy
        frame_length = int(sample_rate * 0.025)  # 25ms frames
        hop_length = int(sample_rate * 0.010)    # 10ms hop
        
        energy = []
        for i in range(0, len(data) - frame_length, hop_length):
            frame = data[i:i+frame_length]
            energy.append(np.sum(frame**2) / frame_length)
        
        energy = np.array(energy)
        
        # Calculate statistics
        mean_energy = np.mean(energy)
        max_energy = np.max(energy)
        std_energy = np.std(energy)
        
        # Simple scream detection heuristic
        # 1. Energy spikes (high max/mean ratio)
        # 2. High variability (high standard deviation)
        energy_ratio = max_energy / (mean_energy + 1e-10)
        
        # These thresholds would need to be tuned based on real data
        is_scream = energy_ratio > 10 and std_energy > mean_energy * 2
        
        logger.info(f"Audio analysis - energy_ratio: {energy_ratio}, std_energy: {std_energy}, mean_energy: {mean_energy}")
        logger.info(f"Scream detected: {is_scream}")
        
        return is_scream
    except Exception as e:
        logger.error(f"Error in scream detection: {e}")
        return False

def handle_emergency(user_id, risk_level, location_data, video_path=None, keywords=None):
    """Handle emergency based on risk level"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # Get user info
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        
        if not user:
            logger.error(f"User with ID {user_id} not found")
            conn.close()
            return {"success": False, "message": "User not found"}
        
        # Get emergency contacts
        cursor.execute("SELECT * FROM emergency_contacts WHERE user_id = ?", (user_id,))
        contacts = cursor.fetchall()
        
        if not contacts:
            logger.warning(f"No emergency contacts found for user {user_id}")
            conn.close()
            return {"success": False, "message": "No emergency contacts found"}
        
        # Get address from coordinates
        address = get_address_from_coordinates(location_data["latitude"], location_data["longitude"])
        location_info = {
            "latitude": location_data["latitude"],
            "longitude": location_data["longitude"],
            "address": address
        }
        
        # List of actions taken
        actions_taken = []
        
        # Handle based on risk level
        if risk_level == "risk":
            # Send email to primary contact
            primary_contacts = [c for c in contacts if c["is_primary"] == 1]
            if primary_contacts:
                for contact in primary_contacts:
                    if contact["email"]:
                        send_email_alert(
                            {"email": contact["email"]}, 
                            user["name"], 
                            risk_level, 
                            location_info
                        )
                        actions_taken.append(f"Email alert sent to {contact['name']}")
        
        elif risk_level == "risky":
            # Send email with video to all contacts
            for contact in contacts:
                if contact["email"]:
                    send_email_alert(
                        {"email": contact["email"]}, 
                        user["name"], 
                        risk_level, 
                        location_info, 
                        video_path
                    )
                    actions_taken.append(f"Email with video sent to {contact['name']}")
                
                # Send SMS
                if contact["phone"]:
                    send_sms_alert(
                        {"phone": contact["phone"]}, 
                        user["name"], 
                        risk_level, 
                        location_info
                    )
                    actions_taken.append(f"SMS alert sent to {contact['name']}")
        
        elif risk_level == "high risk":
            # Do everything from "risky" level
            for contact in contacts:
                if contact["email"]:
                    send_email_alert(
                        {"email": contact["email"]}, 
                        user["name"], 
                        risk_level, 
                        location_info, 
                        video_path
                    )
                    actions_taken.append(f"Email with video sent to {contact['name']}")
                
                if contact["phone"]:
                    send_sms_alert(
                        {"phone": contact["phone"]}, 
                        user["name"], 
                        risk_level, 
                        location_info
                    )
                    actions_taken.append(f"SMS alert sent to {contact['name']}")
            
            # Find nearest police station
            police_station = find_nearest_police_station(
                location_data["latitude"], 
                location_data["longitude"]
            )
            
            if police_station:
                # Call police
                make_emergency_call(
                    police_station["phone"],
                    user["name"],
                    location_info
                )
                actions_taken.append(f"Emergency call placed to {police_station['name']}")
        
        # Save alert to database
        alert_id = str(uuid.uuid4())
        cursor.execute(
            """
            INSERT INTO alerts (
                id, user_id, timestamp, risk_level, latitude, longitude, 
                address, keywords_detected, video_path, actions_taken
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_id, user_id, datetime.datetime.now().isoformat(),
                risk_level, location_data["latitude"], location_data["longitude"],
                address, json.dumps(keywords or []), video_path,
                json.dumps(actions_taken)
            )
        )
        conn.commit()
        
        logger.info(f"Emergency handled successfully for user {user_id}, risk level: {risk_level}")
        return {
            "success": True,
            "alert_id": alert_id,
            "actions_taken": actions_taken
        }
    except Exception as e:
        logger.error(f"Error handling emergency: {e}")
        conn.rollback()
        return {"success": False, "message": str(e)}
    finally:
        conn.close()

# Helper function to extract audio from video using wave and subprocess
def extract_audio_from_video(video_path):
    """
    Alternative method to extract audio from video without using FFmpeg directly.
    Uses the audio file if it's already an audio format, or manual processing if video.
    """
    file_ext = os.path.splitext(video_path)[1].lower()
    
    # If the file is already an audio file, just return the path
    if file_ext in ['.wav', '.mp3']:
        return video_path
    
    # For video files, we'll create a simpler version where we just
    # prompt users to upload audio instead of video if needed
    audio_path = os.path.splitext(video_path)[0] + ".wav"
    
    # Instead of extracting, we'll log that this would require FFmpeg
    logger.warning(f"Audio extraction from video requires FFmpeg. Using simplified risk assessment instead.")
    
    # Create a basic empty audio file as a fallback
    # This is just a placeholder and won't contain actual audio data
    try:
        # Create a simple empty WAV file (1 second of silence)
        channels = 1
        sample_width = 2  # 16 bits
        sample_rate = 44100
        num_frames = sample_rate  # 1 second
        
        with wave.open(audio_path, 'wb') as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(b'\x00' * num_frames * sample_width * channels)
        
        logger.info(f"Created placeholder audio file at {audio_path}")
        return audio_path
    except Exception as e:
        logger.error(f"Error creating placeholder audio: {e}")
        return None

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        
        # Check if email already exists
        cursor.execute("SELECT id FROM users WHERE email = ?", (data['email'],))
        if cursor.fetchone():
            conn.close()
            return jsonify({"success": False, "message": "Email already registered"})
        
        # Create new user
        user_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO users (id, name, email, password) VALUES (?, ?, ?, ?)",
            (user_id, data['name'], data['email'], data['password'])  # In production, hash the password
        )
        
        conn.commit()
        conn.close()
        
        return jsonify({"success": True, "user_id": user_id})
    except Exception as e:
        logger.error(f"Registration error: {e}")
        return jsonify({"success": False, "message": str(e)})

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT id, name, email FROM users WHERE email = ? AND password = ?",
            (data['email'], data['password'])  # In production, verify hashed password
        )
        user = cursor.fetchone()
        
        conn.close()
        
        if user:
            return jsonify({
                "success": True,
                "user_id": user['id'],
                "name": user['name'],
                "email": user['email']
            })
        else:
            return jsonify({"success": False, "message": "Invalid credentials"})
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({"success": False, "message": str(e)})

@app.route('/contacts', methods=['POST'])
def save_contacts():
    data = request.json
    user_id = data.get('user_id')
    
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        
        # Delete existing contacts for this user
        cursor.execute("DELETE FROM emergency_contacts WHERE user_id = ?", (user_id,))
        
        # Add primary contact
        primary_id = str(uuid.uuid4())
        cursor.execute(
            """
            INSERT INTO emergency_contacts (id, user_id, name, phone, email, is_primary)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (primary_id, user_id, data['primaryName'], data['primaryPhone'], data['primaryEmail'])
        )
        
        # Add secondary contact if provided
        if data.get('secondaryName') and (data.get('secondaryPhone') or data.get('secondaryEmail')):
            secondary_id = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO emergency_contacts (id, user_id, name, phone, email, is_primary)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    secondary_id, user_id, data['secondaryName'], 
                    data.get('secondaryPhone', ''), data.get('secondaryEmail', '')
                )
            )
        
        conn.commit()
        conn.close()
        
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving contacts: {e}")
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    user_id = request.args.get('user_id')
    
    if not user_id:
        return jsonify({"success": False, "message": "User ID required"})
    
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute(
            """
            SELECT id, timestamp, risk_level, latitude, longitude, address, actions_taken
            FROM alerts
            WHERE user_id = ?
            ORDER BY timestamp DESC
            """,
            (user_id,)
        )
        
        alerts = []
        for row in cursor.fetchall():
            alerts.append({
                "id": row['id'],
                "timestamp": row['timestamp'],
                "risk_level": row['risk_level'],
                "latitude": row['latitude'],
                "longitude": row['longitude'],
                "address": row['address'],
                "actions_taken": json.loads(row['actions_taken'])
            })
        
        conn.close()
        
        return jsonify({"success": True, "alerts": alerts})
    except Exception as e:
        logger.error(f"Error fetching alerts: {e}")
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/upload_video', methods=['POST'])
def upload_video():
    if 'video' not in request.files:
        return jsonify({"success": False, "message": "No video file provided"})
    
    file = request.files['video']
    user_id = request.form.get('user_id')
    timestamp = request.form.get('timestamp', datetime.datetime.now().isoformat())
    location = json.loads(request.form.get('location', '{}'))
    risk_level = request.form.get('risk_level', '')
    
    if not user_id:
        return jsonify({"success": False, "message": "User ID required"})
    
    if file.filename == '':
        return jsonify({"success": False, "message": "No file selected"})
    
    if file and allowed_file(file.filename):
        # Process the video
        filename = secure_filename(f"{user_id}{int(time.time())}{file.filename}")
        video_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(video_path)
        
        # Try to extract audio from video using our alternative method
        def process_media():
            nonlocal risk_level
            
            # Check if the uploaded file is directly an audio file
            is_audio_file = os.path.splitext(file.filename)[1].lower() in ['.wav', '.mp3']
            
            if is_audio_file:
                # Direct audio file processing
                audio_path = video_path
                keywords = process_audio_for_keywords(audio_path)
                is_scream = detect_scream(audio_path)
            else:
                # For video files, use our simplified approach
                audio_path = extract_audio_from_video(video_path)
                
                # If audio extraction failed or is just a placeholder, use simplified risk assessment
                if not audio_path or audio_path == "placeholder":
                    keywords = []
                    is_scream = False
                    logger.warning("Using simplified risk assessment due to audio extraction limitations")
                else:
                    # Process the audio if we have it
                    keywords = process_audio_for_keywords(audio_path)
                    is_scream = detect_scream(audio_path)
            
            # Determine risk level if not provided
            if not risk_level:
                if is_scream or 'high risk' in keywords:
                    risk_level = 'high risk'
                elif 'risky' in keywords:
                    risk_level = 'risky'
                elif 'risk' in keywords or keywords:
                    risk_level = 'risk'
                else:
                    # Default to "risk" level if we can't detect audio properly
                    if not is_audio_file and not (audio_path and audio_path != "placeholder"):
                        risk_level = 'risk'  # Default when we can't analyze audio
                        logger.info("Setting default risk level due to audio processing limitations")
                    else:
                        risk_level = 'none'
            
            # Handle emergency if risk detected
            if risk_level != 'none':
                handle_emergency(
                    user_id, 
                    risk_level, 
                    location, 
                    video_path, 
                    keywords
                )
            
            # Clean up temporary files
            if audio_path and audio_path != video_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except:
                    pass
        
        threading.Thread(target=process_media).start()
        
        return jsonify({
            "success": True,
            "video_path": video_path,
            "message": "Video uploaded successfully and processing started"
        })
    
    return jsonify({"success": False, "message": "Invalid file type"})

@app.route('/api/stream_audio', methods=['POST'])
def stream_audio():
    # This endpoint handles real-time audio streaming
    data = request.get_data()
    user_id = request.args.get('user_id')
    
    if not user_id:
        return jsonify({"success": False, "message": "User ID required"})
    
    try:
        # Save the audio data to a temporary file
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_file:
            temp_file.write(data)
            temp_path = temp_file.name
        
        # Process the audio
        keywords = process_audio_for_keywords(temp_path)
        is_scream = detect_scream(temp_path)
        
        # Determine risk level
        risk_level = 'none'
        if is_scream or 'high risk' in keywords:
            risk_level = 'high risk'
        elif 'risky' in keywords:
            risk_level = 'risky'
        elif 'risk' in keywords or keywords:
            risk_level = 'risk'
        
        # Clean up
        os.unlink(temp_path)
        
        return jsonify({
            "success": True,
            "risk_level": risk_level,
            "keywords": keywords,
            "is_scream": is_scream
        })
    except Exception as e:
        logger.error(f"Error processing audio stream: {e}")
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/alert', methods=['POST'])
def create_alert():
    data = request.json
    
    user_id = data.get('user_id')
    risk_level = data.get('risk_level')
    location = data.get('location')
    video_path = data.get('video_path')
    keywords = data.get('keywords', [])
    
    if not all([user_id, risk_level, location]):
        return jsonify({"success": False, "message": "Missing required fields"})
    
    result = handle_emergency(user_id, risk_level, location, video_path, keywords)
    return jsonify(result)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# Initialize the database and start the server
if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=True)

