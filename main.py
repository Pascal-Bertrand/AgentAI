import openai
import json
from typing import Dict, Optional, List
import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, send_from_directory
import threading
import webbrowser
from flask_cors import CORS
import base64
import tempfile

# Initialize the OpenAI client with your API key
try:
    # Try loading from .env file first
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    
    # Clean up the API key if it contains newlines or spaces
    if api_key:
        api_key = api_key.replace("\n", "").replace(" ", "").strip()
except ImportError:
    api_key = os.getenv("OPENAI_API_KEY")

client = openai.OpenAI(api_key=api_key)
if not client.api_key:
    raise ValueError("Please set OPENAI_API_KEY in environment variables or .env file")

# Add these constants at the top level
SCOPES = ['https://www.googleapis.com/auth/calendar']
CLIENT_ID = '473172815719-uqsf1bv6rior1ctebkernlnamca3mv3e.apps.googleusercontent.com'
TOKEN_FILE = 'token.pickle'

# Define task structure
class Task:
    def __init__(self, title: str, description: str, due_date: datetime, 
                 assigned_to: str, priority: str, project_id: str):
        self.title = title
        self.description = description
        self.due_date = due_date
        self.assigned_to = assigned_to
        self.priority = priority
        self.project_id = project_id
        self.completed = False
        self.id = f"task_{hash(title + assigned_to + str(due_date))}"
    
    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "due_date": self.due_date.isoformat(),
            "assigned_to": self.assigned_to,
            "priority": self.priority,
            "project_id": self.project_id,
            "completed": self.completed
        }
    
    def __str__(self):
        return f"{self.title} - Due: {self.due_date.strftime('%Y-%m-%d')} - Assigned to: {self.assigned_to}"

class Network:
    def __init__(self, log_file: Optional[str] = None):
        self.nodes: Dict[str, LLMNode] = {}
        self.log_file = log_file
        self.tasks: List[Task] = []

    def register_node(self, node: 'LLMNode'):
        self.nodes[node.node_id] = node
        node.network = self

    def send_message(self, sender_id: str, recipient_id: str, content: str):
        self._log_message(sender_id, recipient_id, content)

        if recipient_id in self.nodes:
            self.nodes[recipient_id].receive_message(content, sender_id)
        else:
            print(f"Node {recipient_id} not found in the network.")

    def _log_message(self, sender_id: str, recipient_id: str, content: str):
        if self.log_file:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"From {sender_id} to {recipient_id}: {content}\n")
    
    def add_task(self, task: Task):
        self.tasks.append(task)
        # Notify the assigned person
        if task.assigned_to in self.nodes:
            message = f"New task assigned: {task.title}. Due: {task.due_date.strftime('%Y-%m-%d')}. Priority: {task.priority}."
            self.send_message("system", task.assigned_to, message)
    
    def get_tasks_for_node(self, node_id: str) -> List[Task]:
        return [task for task in self.tasks if task.assigned_to == node_id]


class LLMNode:
    def __init__(self, node_id: str, knowledge: str = "",
                 llm_api_key: str = "", llm_params: dict = None):
        """
        Node representing a user/agent, each with its own knowledge and mini-world (projects, calendar, etc.).
        """
        self.node_id = node_id
        self.knowledge = knowledge

        # If each node can have its own API key, set it here. Otherwise, use the shared client.
        self.llm_api_key = llm_api_key
        self.client = client if not self.llm_api_key else openai.OpenAI(api_key=self.llm_api_key)

        # Tuning LLM params for concise answers
        self.llm_params = llm_params if llm_params else {
            "model": "gpt-4o",
            "temperature": 0.1,
            "max_tokens": 1000
        }

        # Store conversation if needed
        self.conversation_history = []

        # For multiple projects, store them in a dict: { project_id: {...}, ... }
        self.projects = {}

        # Calendar for meeting scheduling
        self.calendar = []
        # Uncomment calendar service initialization
        self.calendar_service = self._get_calendar_service()

        self.network: Optional[Network] = None

    def _get_calendar_service(self):
        """Initialize Google Calendar service with improved error handling"""
        print(f"[{self.node_id}] Initializing Google Calendar service...")
        
        # Check if client secret is available
        client_secret = os.getenv('GOOGLE_CLIENT_SECRET')
        if not client_secret:
            print(f"[{self.node_id}] ERROR: GOOGLE_CLIENT_SECRET environment variable not found")
            return None
        
        print(f"[{self.node_id}] Client secret found: {client_secret[:5]}...")
        
        creds = None
        if os.path.exists(TOKEN_FILE):
            print(f"[{self.node_id}] Found existing token file")
            try:
                with open(TOKEN_FILE, 'rb') as token:
                    creds = pickle.load(token)
                print(f"[{self.node_id}] Successfully loaded credentials from token file")
            except Exception as e:
                print(f"[{self.node_id}] Error loading token file: {str(e)}")
                # Delete invalid token file
                os.remove(TOKEN_FILE)
                print(f"[{self.node_id}] Deleted invalid token file")
                creds = None
        else:
            print(f"[{self.node_id}] No token file found at {TOKEN_FILE}")
        
        try:
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    try:
                        print(f"[{self.node_id}] Refreshing expired credentials")
                        creds.refresh(Request())
                        print(f"[{self.node_id}] Credentials refreshed successfully")
                    except Exception as e:
                        print(f"[{self.node_id}] Error refreshing credentials: {str(e)}")
                        print(f"[{self.node_id}] Will start new OAuth flow")
                        creds = None
                        # Delete invalid token file if it exists
                        if os.path.exists(TOKEN_FILE):
                            os.remove(TOKEN_FILE)
                            print(f"[{self.node_id}] Deleted invalid token file")
                
                if not creds:
                    print(f"[{self.node_id}] Starting new OAuth flow with client ID: {CLIENT_ID[:10]}...")
                    client_config = {
                        "installed": {
                            "client_id": CLIENT_ID,
                            "client_secret": client_secret,
                            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                            "token_uri": "https://oauth2.googleapis.com/token",
                            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                            "redirect_uris": ["http://localhost:8080/"]
                        }
                    }
                    
                    try:
                        flow = InstalledAppFlow.from_client_config(
                            client_config,
                            scopes=SCOPES
                        )
                        print(f"[{self.node_id}] OAuth flow created successfully")
                        
                        # Generate the authorization URL and open it in a web browser
                        auth_url, _ = flow.authorization_url(prompt='consent')
                        print(f"[{self.node_id}] Opening authorization URL in browser: {auth_url[:60]}...")
                        webbrowser.open(auth_url)
                        
                        print(f"[{self.node_id}] Running local server for authentication on port 8080...")
                        print(f"[{self.node_id}] Please complete the authorization in your browser")
                        creds = flow.run_local_server(port=8080)
                        print(f"[{self.node_id}] Authentication successful")
                    except Exception as e:
                        print(f"[{self.node_id}] Authentication error: {str(e)}")
                        print(f"[{self.node_id}] Full error details: {repr(e)}")
                        return None

                print(f"[{self.node_id}] Saving credentials to token file: {TOKEN_FILE}")
                try:
                    with open(TOKEN_FILE, 'wb') as token:
                        pickle.dump(creds, token)
                    print(f"[{self.node_id}] Credentials saved successfully")
                except Exception as e:
                    print(f"[{self.node_id}] Error saving credentials: {str(e)}")

            print(f"[{self.node_id}] Building calendar service...")
            service = build('calendar', 'v3', credentials=creds)
            
            # Test the service with a simple API call
            print(f"[{self.node_id}] Testing calendar service with calendarList.list()...")
            calendar_list = service.calendarList().list().execute()
            print(f"[{self.node_id}] Calendar service working! Found {len(calendar_list.get('items', []))} calendars")
            
            return service
        except Exception as e:
            print(f"[{self.node_id}] Failed to build or test calendar service: {str(e)}")
            print(f"[{self.node_id}] Full error details: {repr(e)}")
            return None

    # Uncomment the calendar reminder method
    def create_calendar_reminder(self, task: Task):
        """Create a Google Calendar reminder for a task"""
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, skipping reminder creation")
            return
            
        try:
            event = {
                'summary': f"TASK: {task.title}",
                'description': f"{task.description}\n\nPriority: {task.priority}\nProject: {task.project_id}",
                'start': {
                    'dateTime': task.due_date.isoformat(),
                    'timeZone': 'UTC',
                },
                'end': {
                    'dateTime': (task.due_date + timedelta(hours=1)).isoformat(),
                    'timeZone': 'UTC',
                },
                'attendees': [{'email': f'{task.assigned_to}@example.com'}],
                'reminders': {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'email', 'minutes': 24 * 60},  # 1 day before
                        {'method': 'popup', 'minutes': 60}         # 1 hour before
                    ]
                }
            }
            
            event = self.calendar_service.events().insert(calendarId='primary', body=event).execute()
            print(f"[{self.node_id}] Task reminder created: {event.get('htmlLink')}")
            
        except Exception as e:
            print(f"[{self.node_id}] Failed to create calendar reminder: {e}")

    # Replace the local meeting scheduling with Google Calendar version
    def schedule_meeting(self, project_id: str, participants: list):
        """Updated to use Google Calendar with proper current time"""
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, using local scheduling")
            self._fallback_schedule_meeting(project_id, participants)
            return
            
        meeting_description = f"Meeting for project '{project_id}'"
        
        # Use current time properly
        start_time = datetime.now() + timedelta(days=1)
        end_time = start_time + timedelta(hours=1)
        
        # Create event with proper time format
        event = {
            'summary': meeting_description,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'UTC',
            },
            'attendees': [{'email': f'{p}@example.com'} for p in participants],
        }

        try:
            event = self.calendar_service.events().insert(calendarId='primary', body=event).execute()
            print(f"[{self.node_id}] Meeting created: {event.get('htmlLink')}")
            
            # Store in local calendar as well
            self.calendar.append({
                'project_id': project_id,
                'meeting_info': meeting_description,
                'event_id': event['id']
            })

            # Notify other participants
            for p in participants:
                if p != self.node_id and p in self.network.nodes:
                    self.network.nodes[p].calendar.append({
                        'project_id': project_id,
                        'meeting_info': meeting_description,
                        'event_id': event['id']
                    })
                    notification = f"New meeting: '{meeting_description}' scheduled by {self.node_id} for {start_time.strftime('%Y-%m-%d %H:%M')}"
                    self.network.send_message(self.node_id, p, notification)
        except Exception as e:
            print(f"[{self.node_id}] Failed to create calendar event: {e}")
            # Fallback to local calendar
            self._fallback_schedule_meeting(project_id, participants)
    
    # Uncomment the fallback method
    def _fallback_schedule_meeting(self, project_id: str, participants: list):
        """Local fallback for scheduling when Google Calendar fails"""
        meeting_info = f"Meeting for project '{project_id}' scheduled for {datetime.now() + timedelta(days=1)}"
        self.calendar.append({
            'project_id': project_id,
            'meeting_info': meeting_info
        })
        
        print(f"[{self.node_id}] Scheduled local meeting: {meeting_info}")
        
        # Notify other participants
        for p in participants:
            if p in self.network.nodes:
                self.network.nodes[p].calendar.append({
                    'project_id': project_id,
                    'meeting_info': meeting_info
                })
                print(f"[{self.node_id}] Notified {p} about meeting for project '{project_id}'.")

    def receive_message(self, message: str, sender_id: str):
        """More dynamic message handling with conversation state"""
        print(f"[{self.node_id}] Received from {sender_id}: {message}")

        # Check if we're in the middle of gathering meeting information
        if hasattr(self, 'meeting_context') and self.meeting_context.get('active'):
            self._continue_meeting_creation(message, sender_id)
            return

        # Regular message handling
        if sender_id == "cli_user":
            # Detect calendar intent
            calendar_intent = self._detect_calendar_intent(message)
            
            if calendar_intent.get("is_calendar_command", False):
                action = calendar_intent.get("action")
                missing_info = calendar_intent.get("missing_info", [])
                
                if action == "schedule_meeting":
                    # Initialize meeting creation flow if information is missing
                    if missing_info:
                        self._start_meeting_creation(message, missing_info)
                    else:
                        self._handle_meeting_creation(message)
                    return
                elif action == "cancel_meeting":
                    self._handle_meeting_cancellation(message)
                    return
                elif action == "list_meetings":
                    self._handle_list_meetings()
                    return
                elif action == "reschedule_meeting":
                    self._handle_meeting_rescheduling(message)
                    return

        # Regular message handling (unchanged)
        self.conversation_history.append({"role": "user", "content": f"{sender_id} says: {message}"})
        if sender_id == "cli_user":
            response = self.query_llm(self.conversation_history)
            self.conversation_history.append({"role": "assistant", "content": response})
            print(f"[{self.node_id}] Response: {response}")

    def _detect_calendar_intent(self, message):
        """Simplified calendar intent detection"""
        prompt = f"""
        Analyze this message and determine if it's a calendar-related command:
        "{message}"
        
        Return JSON with:
        - is_calendar_command: boolean
        - action: string ("schedule_meeting", "cancel_meeting", "list_meetings", "reschedule_meeting", or null)
        - missing_info: array of strings (what information is missing: "time", "participants", "date", "title")
        """
        
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"[{self.node_id}] Error detecting intent: {str(e)}")
            return {"is_calendar_command": False, "action": None, "missing_info": []}

    def _start_meeting_creation(self, initial_message, missing_info):
        """Start the meeting creation flow by asking for missing information"""
        # Initialize meeting context
        self.meeting_context = {
            'active': True,
            'initial_message': initial_message,
            'missing_info': missing_info.copy(),
            'collected_info': {}
        }
        
        # Ask for the first missing piece of information
        self._ask_for_next_meeting_info()

    def _ask_for_next_meeting_info(self):
        """Ask user for the next piece of missing meeting information"""
        if not self.meeting_context['missing_info']:
            # We have all the information, proceed with meeting creation
            combined_message = self._construct_complete_meeting_message()
            self._handle_meeting_creation(combined_message)
            self.meeting_context['active'] = False
            return
        
        next_info = self.meeting_context['missing_info'][0]
        
        # Improved questions with better guidance
        questions = {
            'time': "What time should the meeting be scheduled? (Please use HH:MM format in 24-hour time, e.g., 14:30)",
            'date': "On what date should the meeting be scheduled? (Please use YYYY-MM-DD format, e.g., 2023-12-31)",
            'participants': "Who should attend the meeting? Please list all participants.",
            'title': "What is the title or topic of the meeting?"
        }
        
        # Add context if rescheduling
        context = ""
        if self.meeting_context.get('is_rescheduling', False):
            context = " for rescheduling"
        elif next_info in ['date', 'time'] and 'date' in self.meeting_context['missing_info'] and 'time' in self.meeting_context['missing_info']:
            context = " (please ensure it's a future date and time)"
        
        response = questions.get(next_info, f"Please provide the {next_info} for the meeting") + context
        print(f"[{self.node_id}] Response: {response}")

    def _continue_meeting_creation(self, message, sender_id):
        """Process user's response to our question about meeting details"""
        if not self.meeting_context['missing_info']:
            # Shouldn't happen, but just in case
            self.meeting_context['active'] = False
            return
        
        current_info = self.meeting_context['missing_info'].pop(0)
        self.meeting_context['collected_info'][current_info] = message
        
        if self.meeting_context['missing_info']:
            # Still need more information
            self._ask_for_next_meeting_info()
        else:
            # We have all the information
            if self.meeting_context.get('is_rescheduling', False) and 'target_event_id' in self.meeting_context:
                # Handle rescheduling completion
                self._complete_meeting_rescheduling()
            else:
                # Handle regular meeting creation
                combined_message = self._construct_complete_meeting_message()
                self._handle_meeting_creation(combined_message)
            
            self.meeting_context['active'] = False
            print(f"[{self.node_id}] Response: Meeting {'rescheduled' if self.meeting_context.get('is_rescheduling') else 'scheduled'} successfully with all required information.")

    def _construct_complete_meeting_message(self):
        """Combine initial message with collected information into a complete instruction"""
        initial = self.meeting_context['initial_message']
        collected = self.meeting_context['collected_info']
        
        # Create a complete message with all the information
        complete_message = f"{initial} "
        if 'title' in collected:
            complete_message += f"Title: {collected['title']}. "
        if 'date' in collected:
            complete_message += f"Date: {collected['date']}. "
        if 'time' in collected:
            complete_message += f"Time: {collected['time']}. "
        if 'participants' in collected:
            complete_message += f"Participants: {collected['participants']}."
        
        return complete_message

    def _handle_meeting_creation(self, message):
        """Meeting creation with improved time validation and interaction"""
        # Extract meeting details
        meeting_data = self._extract_meeting_details(message)
        
        # Validate that we have all required information
        required_fields = ['title', 'participants']
        missing = [field for field in required_fields if not meeting_data.get(field)]
        
        if missing:
            print(f"[{self.node_id}] Cannot schedule meeting: missing {', '.join(missing)}")
            return
        
        # Process participants
        participants = []
        for p in meeting_data.get("participants", []):
            p_lower = p.lower().strip()
            if p_lower in ["ceo", "marketing", "engineering", "design"]:
                participants.append(p_lower)
        
        # Ensure we have participants
        if not participants:
            print(f"[{self.node_id}] Cannot schedule meeting: no valid participants")
            return
            
        # Add the current node if not already included
        if self.node_id not in participants:
            participants.append(self.node_id)
        
        # Process date/time
        meeting_date = meeting_data.get("date", datetime.now().strftime("%Y-%m-%d"))
        meeting_time = meeting_data.get("time", (datetime.now() + timedelta(hours=1)).strftime("%H:%M"))
        
        try:
            # Validate date format 
            try:
                start_datetime = datetime.strptime(f"{meeting_date} {meeting_time}", "%Y-%m-%d %H:%M")
                
                # Check if date is in the past
                current_time = datetime.now()
                if start_datetime < current_time:
                    # Instead of automatically adjusting, ask the user for a valid time
                    print(f"[{self.node_id}] Response: The meeting time {meeting_date} at {meeting_time} is in the past. Please provide a future date and time.")
                    
                    # Store context for follow-up
                    self.meeting_context = {
                        'active': True,
                        'collected_info': {
                            'title': meeting_data.get("title"),
                            'participants': meeting_data.get("participants", [])
                        },
                        'missing_info': ['date', 'time'],
                        'is_rescheduling': False
                    }
                    
                    # Ask for new date and time
                    self._ask_for_next_meeting_info()
                    return
                
            except ValueError:
                # If date parsing fails, notify user instead of auto-fixing
                print(f"[{self.node_id}] Response: I couldn't understand the date/time format. Please provide the date in YYYY-MM-DD format and time in HH:MM format.")
                
                # Store context for follow-up
                self.meeting_context = {
                    'active': True,
                    'collected_info': {
                        'title': meeting_data.get("title"),
                        'participants': meeting_data.get("participants", [])
                    },
                    'missing_info': ['date', 'time'],
                    'is_rescheduling': False
                }
                
                # Ask for new date and time
                self._ask_for_next_meeting_info()
                return
            
            duration_mins = int(meeting_data.get("duration", 60))
            end_datetime = start_datetime + timedelta(minutes=duration_mins)
            
            # Create a unique ID and get title
            meeting_id = f"meeting_{int(datetime.now().timestamp())}"
            meeting_title = meeting_data.get("title", f"Meeting scheduled by {self.node_id}")
            
            # Schedule the meeting
            self._create_calendar_meeting(meeting_id, meeting_title, participants, start_datetime, end_datetime)
            
            # Confirm to user with reliable times
            print(f"[{self.node_id}] Meeting '{meeting_title}' scheduled for {meeting_date} at {meeting_time} with {', '.join(participants)}")
        except Exception as e:
            print(f"[{self.node_id}] Error creating meeting: {str(e)}")

    def _extract_meeting_details(self, message):
        """Extract meeting details with improved accuracy and defaulting to current time"""
        prompt = f"""
        Extract complete meeting details from: "{message}"
        
        Return JSON with:
        - title: meeting title
        - participants: array of participants (use only: ceo, marketing, engineering, design)
        - date: meeting date (YYYY-MM-DD format, leave empty to use current date)
        - time: meeting time (HH:MM format, leave empty to use current time + 1 hour)
        - duration: duration in minutes (default 60)
        
        If any information is missing, leave the field empty (don't guess).
        """
        
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            
            # Use current date if not specified
            if not result.get("date"):
                result["date"] = datetime.now().strftime("%Y-%m-%d")
            
            # Use current time + 1 hour if not specified
            if not result.get("time"):
                result["time"] = (datetime.now() + timedelta(hours=1)).strftime("%H:%M")
            
            return result
        except Exception as e:
            print(f"[{self.node_id}] Error extracting meeting details: {str(e)}")
            return {}

    def _handle_list_meetings(self):
        """Handle request to list upcoming meetings"""
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, showing local meetings only")
            if not self.calendar:
                print(f"[{self.node_id}] No meetings scheduled.")
                return
            
        try:
            # Get upcoming meetings from Google Calendar
            now = datetime.utcnow().isoformat() + 'Z'
            events_result = self.calendar_service.events().list(
                calendarId='primary',
                timeMin=now,
                maxResults=10,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])
            
            if not events:
                print(f"[{self.node_id}] No upcoming meetings found.")
                return
            
            print(f"[{self.node_id}] Upcoming meetings:")
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                start_time = datetime.fromisoformat(start.replace('Z', '+00:00'))
                attendees = ", ".join([a.get('email', '').split('@')[0] for a in event.get('attendees', [])])
                print(f"  - {event['summary']} on {start_time.strftime('%Y-%m-%d at %H:%M')} with {attendees}")
            
        except Exception as e:
            print(f"[{self.node_id}] Error listing meetings: {str(e)}")

    def _handle_meeting_rescheduling(self, message):
        """Handle meeting rescheduling with proper event updating"""
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, can't reschedule meetings")
            return
        
        try:
            # Use OpenAI to extract rescheduling details with more explicit prompt
            prompt = f"""
            Extract meeting rescheduling details from this message: "{message}"
            
            Identify EXACTLY which meeting needs rescheduling by looking for:
            1. Meeting title or topic (as a simple text string)
            2. Participants involved (as names only)
            3. Original date/time
            
            And what the new schedule should be:
            1. New date (YYYY-MM-DD format)
            2. New time (HH:MM format in 24-hour time)
            3. New duration in minutes (as a number only)
            
            Return a JSON object with these fields:
            - meeting_identifier: A simple text string to identify which meeting to reschedule
            - original_date: Original meeting date if mentioned (YYYY-MM-DD format or null)
            - new_date: New meeting date (YYYY-MM-DD format)
            - new_time: New meeting time (HH:MM format)
            - new_duration: New duration in minutes (or null to keep the same)
            
            IMPORTANT: ALL values must be simple strings or integers, not objects or arrays.
            The meeting_identifier MUST be a simple string.
            """
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            # Extract and validate the rescheduling data (keep existing code for this part)
            response_content = response.choices[0].message.content
            try:
                reschedule_data = json.loads(response_content)
            except json.JSONDecodeError as e:
                print(f"[{self.node_id}] Error parsing rescheduling JSON: {e}")
                return
            
            # Defensive extraction of data with type checking (keep existing code)
            meeting_identifier = ""
            if "meeting_identifier" in reschedule_data:
                if isinstance(reschedule_data["meeting_identifier"], str):
                    meeting_identifier = reschedule_data["meeting_identifier"].lower()
                else:
                    meeting_identifier = str(reschedule_data["meeting_identifier"]).lower()
            
            original_date = None
            if "original_date" in reschedule_data and reschedule_data["original_date"]:
                original_date = str(reschedule_data["original_date"])
            
            new_date = None
            if "new_date" in reschedule_data and reschedule_data["new_date"]:
                new_date = str(reschedule_data["new_date"])
            
            new_time = "10:00"  # Default time
            if "new_time" in reschedule_data and reschedule_data["new_time"]:
                new_time = str(reschedule_data["new_time"])
            
            new_duration = None
            if "new_duration" in reschedule_data and reschedule_data["new_duration"]:
                try:
                    new_duration = int(reschedule_data["new_duration"])
                except (ValueError, TypeError):
                    new_duration = None
            
            # Validation checks (keep existing code)
            if not meeting_identifier:
                print(f"[{self.node_id}] Could not determine which meeting to reschedule")
                return
            
            if not new_date:
                print(f"[{self.node_id}] No new date specified for rescheduling")
                return
            
            # Get upcoming meetings
            try:
                now = datetime.utcnow().isoformat() + 'Z'
                events_result = self.calendar_service.events().list(
                    calendarId='primary',
                    timeMin=now,
                    maxResults=20,
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
                events = events_result.get('items', [])
            except Exception as e:
                print(f"[{self.node_id}] Error fetching calendar events: {str(e)}")
                return
            
            if not events:
                print(f"[{self.node_id}] No upcoming meetings found to reschedule")
                return
            
            # Find the meeting to reschedule (keep scoring system code)
            target_event = None
            best_match_score = 0
            
            for event in events:
                score = 0
                
                # Check title match
                event_title = event.get('summary', '').lower()
                if meeting_identifier in event_title:
                    score += 3
                elif any(word in event_title for word in meeting_identifier.split()):
                    score += 1
                
                # Check attendees match
                attendees = []
                for attendee in event.get('attendees', []):
                    email = attendee.get('email', '')
                    if isinstance(email, str):
                        attendees.append(email.lower())
                    else:
                        attendees.append(str(email).lower())
                    
                if any(meeting_identifier in attendee for attendee in attendees):
                    score += 2
                
                # Check date match if original date was specified
                if original_date:
                    start_time = event['start'].get('dateTime', event['start'].get('date', ''))
                    if isinstance(start_time, str) and original_date in start_time:
                        score += 4
                
                # Update best match if this is better
                if score > best_match_score:
                    best_match_score = score
                    target_event = event
            
            # Require a minimum matching score
            if best_match_score < 1:
                print(f"[{self.node_id}] Could not find a meeting matching '{meeting_identifier}'")
                return
            
            if not target_event:
                print(f"[{self.node_id}] No matching meeting found for '{meeting_identifier}'")
                return
            
            # Validate the new date and time
            try:
                # Parse new date and time
                new_start_datetime = datetime.strptime(f"{new_date} {new_time}", "%Y-%m-%d %H:%M")
                
                # Check if date is in the past
                if new_start_datetime < datetime.now():
                    print(f"[{self.node_id}] Response: The rescheduled time {new_date} at {new_time} is in the past. Please provide a future date and time.")
                    
                    # Ask for new date and time
                    self.meeting_context = {
                        'active': True,
                        'collected_info': {
                            'title': target_event.get('summary', 'Meeting'),  # Keep original title
                            'participants': []  # We'll keep the same participants
                        },
                        'missing_info': ['date', 'time'],
                        'is_rescheduling': True,
                        'target_event_id': target_event['id'],
                        'target_event': target_event  # Store the whole event to preserve details
                    }
                    
                    self._ask_for_next_meeting_info()
                    return
            except ValueError:
                print(f"[{self.node_id}] Response: I couldn't understand the date/time format. Please provide the date in YYYY-MM-DD format and time in HH:MM format.")
                
                # Ask for new date and time
                self.meeting_context = {
                    'active': True,
                    'collected_info': {
                        'title': target_event.get('summary', 'Meeting'),  # Keep original title
                        'participants': []  # We'll keep the same participants
                    },
                    'missing_info': ['date', 'time'],
                    'is_rescheduling': True,
                    'target_event_id': target_event['id'],
                    'target_event': target_event  # Store the whole event to preserve details
                }
                
                self._ask_for_next_meeting_info()
                return
            
            # Calculate new end time based on original duration
            try:
                # Extract original start and end times
                original_start = datetime.fromisoformat(target_event['start'].get('dateTime').replace('Z', '+00:00'))
                original_end = datetime.fromisoformat(target_event['end'].get('dateTime').replace('Z', '+00:00'))
                original_duration = (original_end - original_start).total_seconds() / 60
                
                # Use new duration if specified, otherwise keep original duration
                if new_duration is not None and new_duration > 0:
                    duration_to_use = new_duration
                else:
                    duration_to_use = original_duration
                    
                new_end_datetime = new_start_datetime + timedelta(minutes=duration_to_use)
                
                # Update the event with all original data preserved
                target_event['start']['dateTime'] = new_start_datetime.isoformat()
                target_event['end']['dateTime'] = new_end_datetime.isoformat()
                
                # Update event in Google Calendar
                updated_event = self.calendar_service.events().update(
                    calendarId='primary',
                    eventId=target_event['id'],
                    body=target_event
                ).execute()
                
                # Print success message with user-friendly time format
                meeting_title = updated_event.get('summary', 'Untitled meeting')
                formatted_time = new_start_datetime.strftime("%I:%M %p")  # 12-hour format with AM/PM
                formatted_date = new_start_datetime.strftime("%B %d, %Y")  # Month day, year
                
                print(f"[{self.node_id}] Response: Meeting '{meeting_title}' has been rescheduled to {formatted_date} at {formatted_time}.")
                
                # Update local calendar records
                for meeting in self.calendar:
                    if meeting.get('event_id') == updated_event['id']:
                        meeting['meeting_info'] = f"{meeting_title} (Rescheduled to {new_date} at {formatted_time})"
                
                # Notify participants
                attendees = updated_event.get('attendees', [])
                for attendee in attendees:
                    attendee_id = attendee.get('email', '').split('@')[0]
                    if attendee_id in self.network.nodes:
                        # Update their local calendar
                        for meeting in self.network.nodes[attendee_id].calendar:
                            if meeting.get('event_id') == updated_event['id']:
                                meeting['meeting_info'] = f"{meeting_title} (Rescheduled to {new_date} at {formatted_time})"
                        
                        # Send notification
                        notification = (
                            f"Your meeting '{meeting_title}' has been rescheduled by {self.node_id}.\n"
                            f"New date: {formatted_date}\n"
                            f"New time: {formatted_time}\n"
                            f"Duration: {int(duration_to_use)} minutes"
                        )
                        self.network.send_message(self.node_id, attendee_id, notification)
                
            except Exception as e:
                print(f"[{self.node_id}] Error updating the meeting: {str(e)}")
                print(f"[{self.node_id}] Response: There was an error rescheduling the meeting. Please try again.")
            
        except Exception as e:
            print(f"[{self.node_id}] General error in meeting rescheduling: {str(e)}")

    def send_message(self, recipient_id: str, content: str):
        if not self.network:
            print(f"[{self.node_id}] No network attached.")
            return
        
        # Special case for CLI user
        if recipient_id == "cli_user":
            print(f"[{self.node_id}] Response: {content}")
        else:
            self.network.send_message(self.node_id, recipient_id, content)

    def query_llm(self, messages):
        """
        We'll use a system prompt that instructs the LLM to be short, direct, and not loop forever.
        """
        system_prompt = [{
            "role": "system",
            "content": (
                "You are a direct and concise AI agent for an organization. "
                "Provide short, to-the-point answers and do not continue repeating Goodbyes. "
                "End after conveying necessary information."
            )
        }]

        combined_messages = system_prompt + messages
        try:
            completion = self.client.chat.completions.create(
                model=self.llm_params["model"],
                messages=combined_messages,
                temperature=self.llm_params["temperature"],
                max_tokens=self.llm_params["max_tokens"]
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            print(f"[{self.node_id}] LLM query failed: {e}")
            return "LLM query failed."

    def plan_project(self, project_id: str, objective: str):
        """
        Create a detailed project plan, parse it, notify roles, then schedule a meeting for them.
        """
        if project_id not in self.projects:
            self.projects[project_id] = {
                "name": objective,
                "plan": [],
                "participants": set()
            }

        plan_prompt = f"""
        You are creating a detailed project plan for project '{project_id}'.
        Objective: {objective}

        The plan should include:
        1. All stakeholders involved in the project. Use only these roles: CEO, Marketing, Engineering, Design.
        2. Detailed steps needed to execute the plan, including time and cost estimates.
        Each step should be written in paragraphs and full sentences.

        Return valid JSON only, with this structure:
        {{
          "stakeholders": ["list of stakeholders"],
          "steps": [
            {{
              "description": "Detailed step description with time and cost estimates"
            }}
          ]
        }}
        Keep it concise. End after providing the JSON. No extra words.
        """

        response = self.query_llm([{"role": "user", "content": plan_prompt}])
        print(f"[{self.node_id}] LLM raw response (project '{project_id}'): {response}")

        try:
            data = json.loads(response)
            stakeholders = data.get("stakeholders", [])
            steps = data.get("steps", [])
            self.projects[project_id]["plan"] = steps

            # Write the plan to a text file
            with open(f"{project_id}_plan.txt", "w", encoding="utf-8") as file:
                file.write(f"Project ID: {project_id}\n")
                file.write(f"Objective: {objective}\n")
                file.write("Stakeholders:\n")
                for stakeholder in stakeholders:
                    file.write(f"  - {stakeholder}\n")
                file.write("Steps:\n")
                for step in steps:
                    file.write(f"  - {step.get('description', '')}\n")

            # Improved role mapping with case-insensitive matching
            role_to_node = {
                "ceo": "ceo",
                "marketing": "marketing",
                "engineering": "engineering",
                "design": "design"
            }

            participants = []
            for stakeholder in stakeholders:
                # Normalize the role name (lowercase and remove extra spaces)
                role = stakeholder.lower().strip()
                
                # Check for partial matches
                matched = False
                for key in role_to_node:
                    if key in role:
                        node_id = role_to_node[key]
                        participants.append(node_id)
                        self.projects[project_id]["participants"].add(node_id)
                        matched = True
                        break
                
                if not matched:
                    print(f"[{self.node_id}] No mapping for stakeholder '{stakeholder}'. Skipping.")

            print(f"[{self.node_id}] Project participants: {participants}")
            
            # Schedule a meeting
            self.schedule_meeting(project_id, participants)
            
            # Generate tasks from the plan
            self.generate_tasks_from_plan(project_id, steps, participants)
            
        except json.JSONDecodeError as e:
            print(f"[{self.node_id}] Failed to parse JSON plan: {e}")

    def generate_tasks_from_plan(self, project_id: str, steps: list, participants: list):
        """Generate tasks from project plan steps using OpenAI function calling"""
        
        # Define the function for task creation
        functions = [
            {
                "type": "function",
                "function": {
                    "name": "create_task",
                    "description": "Create a task from a project step",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Short title for the task"
                            },
                            "description": {
                                "type": "string",
                                "description": "Detailed description of what needs to be done"
                            },
                            "assigned_to": {
                                "type": "string",
                                "description": "Role responsible for this task (marketing, engineering, design, ceo)"
                            },
                            "due_date_offset": {
                                "type": "integer",
                                "description": "Days from now when the task is due"
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "Priority level of the task"
                            }
                        },
                        "required": ["title", "description", "assigned_to", "due_date_offset", "priority"]
                    }
                }
            }
        ]
        
        # For each step, generate tasks
        for i, step in enumerate(steps):
            step_description = step.get("description", "")
            
            prompt = f"""
            For project '{project_id}', analyze this step and create appropriate tasks:
            
            Step: {step_description}
            
            Available roles: {', '.join(participants)}
            
            Create 1-3 specific tasks from this step. Each task should be assigned to the most appropriate role.
            """
            
            try:
                response = self.client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    tools=functions,
                    tool_choice={"type": "function", "function": {"name": "create_task"}}
                )
                
                # Process the function calls
                for choice in response.choices:
                    if hasattr(choice.message, 'tool_calls') and choice.message.tool_calls:
                        for tool_call in choice.message.tool_calls:
                            if tool_call.function.name == "create_task":
                                task_data = json.loads(tool_call.function.arguments)
                                
                                # Create the task
                                due_date = datetime.now() + timedelta(days=task_data["due_date_offset"])
                                task = Task(
                                    title=task_data["title"],
                                    description=task_data["description"],
                                    due_date=due_date,
                                    assigned_to=task_data["assigned_to"],
                                    priority=task_data["priority"],
                                    project_id=project_id
                                )
                                
                                # Add to network tasks
                                if self.network:
                                    self.network.add_task(task)
                                    print(f"[{self.node_id}] Created task: {task}")
                                    
                                    # Uncomment the calendar reminder
                                    self.create_calendar_reminder(task)
            
            except Exception as e:
                print(f"[{self.node_id}] Error generating tasks for step {i+1}: {e}")

    def list_tasks(self):
        """List all tasks assigned to this node"""
        if not self.network:
            return "No network connected."
            
        tasks = self.network.get_tasks_for_node(self.node_id)
        if not tasks:
            return f"No tasks assigned to {self.node_id}."
            
        result = f"Tasks for {self.node_id}:\n"
        for i, task in enumerate(tasks, 1):
            result += f"{i}. {task.title} (Due: {task.due_date.strftime('%Y-%m-%d')}, Priority: {task.priority})\n"
            result += f"   Description: {task.description}\n"
            
        return result

    def _handle_meeting_cancellation(self, message):
        """Handle natural language meeting cancellation requests"""
        # First, get all meetings from calendar
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, can't cancel meetings")
            return
        
        try:
            # Use OpenAI to extract cancellation details
            prompt = f"""
            Extract meeting cancellation details from this message: "{message}"
            
            Return a JSON object with these fields:
            - title: The meeting title or topic to cancel (or null if not specified)
            - with_participants: Array of participants in the meeting to cancel (or empty if not specified)
            - date: Meeting date to cancel (YYYY-MM-DD format, or null if not specified)
            
            Only include information that is explicitly mentioned.
            """
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            cancel_data = json.loads(response.choices[0].message.content)
            
            # Get upcoming meetings
            now = datetime.utcnow().isoformat() + 'Z'
            events_result = self.calendar_service.events().list(
                calendarId='primary',
                timeMin=now,
                maxResults=10,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])
            
            if not events:
                print(f"[{self.node_id}] No upcoming meetings found to cancel")
                return
            
            # Filter events based on cancellation criteria
            title_filter = cancel_data.get("title")
            participants_filter = [p.lower() for p in cancel_data.get("with_participants", [])]
            date_filter = cancel_data.get("date")
            
            cancelled_count = 0
            for event in events:
                should_cancel = True
                
                # Check title match if specified
                if title_filter and title_filter.lower() not in event.get('summary', '').lower():
                    should_cancel = False
                
                # Check participants if specified
                if participants_filter:
                    event_attendees = [a.get('email', '').split('@')[0].lower() 
                                      for a in event.get('attendees', [])]
                    if not any(p in event_attendees for p in participants_filter):
                        should_cancel = False
                
                # Check date if specified
                if date_filter:
                    event_start = event.get('start', {}).get('dateTime', event.get('start', {}).get('date'))
                    if event_start and date_filter not in event_start:
                        should_cancel = False
                
                if should_cancel:
                    # Cancel the meeting
                    self.calendar_service.events().delete(
                        calendarId='primary',
                        eventId=event['id']
                    ).execute()
                    
                    # Remove from local calendar
                    self.calendar = [m for m in self.calendar if m.get('event_id') != event['id']]
                    
                    # Notify participants
                    event_attendees = [a.get('email', '').split('@')[0] for a in event.get('attendees', [])]
                    for attendee in event_attendees:
                        if attendee in self.network.nodes:
                            # Update their local calendar
                            self.network.nodes[attendee].calendar = [
                                m for m in self.network.nodes[attendee].calendar 
                                if m.get('event_id') != event['id']
                            ]
                            # Notify them
                            notification = f"Meeting '{event.get('summary')}' has been cancelled by {self.node_id}"
                            self.network.send_message(self.node_id, attendee, notification)
                
                    cancelled_count += 1
                    print(f"[{self.node_id}] Cancelled meeting: {event.get('summary')}")
            
            if cancelled_count == 0:
                print(f"[{self.node_id}] No meetings found matching the cancellation criteria")
            else:
                print(f"[{self.node_id}] Cancelled {cancelled_count} meeting(s)")
            
        except Exception as e:
            print(f"[{self.node_id}] Error cancelling meeting: {str(e)}")

    def _create_calendar_meeting(self, meeting_id, title, participants, start_datetime, end_datetime):
        """Create a calendar meeting with the specified details"""
        # If calendar service is not available, fall back to local scheduling
        if not self.calendar_service:
            print(f"[{self.node_id}] Calendar service not available, using local scheduling")
            self._fallback_schedule_meeting(meeting_id, participants)
            return
        
        # Create event
        event = {
            'summary': title,
            'start': {
                'dateTime': start_datetime.isoformat(),
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': end_datetime.isoformat(),
                'timeZone': 'UTC',
            },
            'attendees': [{'email': f'{p}@example.com'} for p in participants],
        }

        try:
            event = self.calendar_service.events().insert(calendarId='primary', body=event).execute()
            
            # Correctly format date and time for user display
            meeting_date = start_datetime.strftime("%Y-%m-%d")
            meeting_time = start_datetime.strftime("%H:%M")
            
            print(f"[{self.node_id}] Meeting created: {event.get('htmlLink')}")
            print(f"[{self.node_id}] Meeting '{title}' scheduled for {meeting_date} at {meeting_time} with {', '.join(participants)}")
            
            # Store in local calendar as well
            self.calendar.append({
                'project_id': meeting_id,
                'meeting_info': title,
                'event_id': event['id']
            })

            # Notify other participants
            for p in participants:
                if p != self.node_id and p in self.network.nodes:
                    self.network.nodes[p].calendar.append({
                        'project_id': meeting_id,
                        'meeting_info': title,
                        'event_id': event['id']
                    })
                    notification = f"New meeting: '{title}' scheduled by {self.node_id} for {meeting_date} at {meeting_time}"
                    self.network.send_message(self.node_id, p, notification)
        except Exception as e:
            print(f"[{self.node_id}] Failed to create calendar event: {e}")
            # Fallback to local calendar
            self._fallback_schedule_meeting(meeting_id, participants)

    def _complete_meeting_rescheduling(self):
        """Complete the meeting rescheduling with the collected information"""
        if not hasattr(self, 'meeting_context') or not self.meeting_context.get('active'):
            return
        
        # Get the new date and time
        new_date = self.meeting_context['collected_info'].get('date')
        new_time = self.meeting_context['collected_info'].get('time')
        target_event_id = self.meeting_context.get('target_event_id')
        
        try:
            # Get the full event
            event = self.calendar_service.events().get(
                calendarId='primary',
                eventId=target_event_id
            ).execute()
            
            # Parse the new date and time
            new_start_datetime = datetime.strptime(f"{new_date} {new_time}", "%Y-%m-%d %H:%M")
            
            # Check if it's still in the past
            if new_start_datetime < datetime.now():
                print(f"[{self.node_id}] The provided time is still in the past. Adjusting to tomorrow at the same time.")
                tomorrow = datetime.now() + timedelta(days=1)
                new_start_datetime = datetime(
                    tomorrow.year, tomorrow.month, tomorrow.day,
                    new_start_datetime.hour, new_start_datetime.minute
                )
            
            # Calculate end time based on original duration
            original_start = datetime.fromisoformat(event['start'].get('dateTime').replace('Z', '+00:00'))
            original_end = datetime.fromisoformat(event['end'].get('dateTime').replace('Z', '+00:00'))
            original_duration = (original_end - original_start).total_seconds() / 60
            
            new_end_datetime = new_start_datetime + timedelta(minutes=original_duration)
            
            # Update the event times while preserving all other data
            event['start']['dateTime'] = new_start_datetime.isoformat()
            event['end']['dateTime'] = new_end_datetime.isoformat()
            
            # Update event in Google Calendar
            updated_event = self.calendar_service.events().update(
                calendarId='primary',
                eventId=target_event_id,
                body=event
            ).execute()
            
            # Format date and time for user-friendly display
            meeting_title = updated_event.get('summary', 'Untitled meeting')
            formatted_time = new_start_datetime.strftime("%I:%M %p")
            formatted_date = new_start_datetime.strftime("%B %d, %Y")
            
            # Success message
            print(f"[{self.node_id}] Response: Meeting '{meeting_title}' has been rescheduled to {formatted_date} at {formatted_time}.")
            
            # Update local calendar records and notify participants
            for meeting in self.calendar:
                if meeting.get('event_id') == updated_event['id']:
                    meeting['meeting_info'] = f"{meeting_title} (Rescheduled to {formatted_date} at {formatted_time})"
            
            # Notify attendees
            attendees = updated_event.get('attendees', [])
            for attendee in attendees:
                attendee_id = attendee.get('email', '').split('@')[0]
                if attendee_id in self.network.nodes:
                    # Update their local calendar
                    for meeting in self.network.nodes[attendee_id].calendar:
                        if meeting.get('event_id') == updated_event['id']:
                            meeting['meeting_info'] = f"{meeting_title} (Rescheduled to {formatted_date} at {formatted_time})"
                    
                    # Send notification
                    notification = (
                        f"Your meeting '{meeting_title}' has been rescheduled by {self.node_id}.\n"
                        f"New date: {formatted_date}\n"
                        f"New time: {formatted_time}"
                    )
                    self.network.send_message(self.node_id, attendee_id, notification)
        
        except Exception as e:
            print(f"[{self.node_id}] Error completing meeting rescheduling: {str(e)}")
            print(f"[{self.node_id}] Response: There was an error rescheduling the meeting. Please try again.")


def run_cli(network):
    print("Commands:\n"
          "  node_id: message => send 'message' to 'node_id' from CLI\n"
          "  node_id: plan project_name = objective => create a new project plan\n"
          "  node_id: tasks => list tasks for a node\n"
          "  quit => exit\n")

    while True:
        user_input = input("> ")
        if user_input.lower().strip() == "quit":
            print("Exiting chat...\n")
            print("\n===== Final State of Each Node =====")
            for node_id, node in network.nodes.items():
                print(f"\n--- Node: {node_id} ---")
                print("Calendar:", node.calendar)
                print("Projects:", node.projects)
                print("Tasks:", network.get_tasks_for_node(node_id))
                print("Conversation History:", node.conversation_history)
            break

        # Plan project command
        if "plan" in user_input and "=" in user_input:
            try:
                # e.g. "ceo: plan p123 = Build AI feature"
                parts = user_input.split(":", 1)
                if len(parts) != 2:
                    print("Invalid format. Use: node_id: plan project_name = objective")
                    continue
                    
                node_id = parts[0].strip()
                command_part = parts[1].strip()
                
                # Extract everything after "plan" keyword
                if "plan" not in command_part:
                    print("Command must include the word 'plan'")
                    continue
                    
                plan_part = command_part.split("plan", 1)[1].strip()
                
                if "=" not in plan_part:
                    print("Invalid format. Missing '=' between project name and objective")
                    continue
                    
                project_id_part, objective_part = plan_part.split("=", 1)
                project_id = project_id_part.strip()
                objective = objective_part.strip()

                if node_id in network.nodes:
                    network.nodes[node_id].plan_project(project_id, objective)
                else:
                    print(f"No node found: {node_id}")
            except Exception as e:
                print(f"Error parsing plan command: {str(e)}")
        # List tasks command
        elif "tasks" in user_input:
            try:
                node_id = user_input.split(":", 1)[0].strip()
                if node_id in network.nodes:
                    tasks_list = network.nodes[node_id].list_tasks()
                    print(tasks_list)
                else:
                    print(f"No node found: {node_id}")
            except Exception as e:
                print(f"Error listing tasks: {e}")
        else:
            # normal message command: "node_id: some message"
            if ":" not in user_input:
                print("Invalid format. Use:\n  node_id: message\nOR\n  node_id: plan project_name = objective\nOR\n  node_id: tasks\n")
                continue
            node_id, message = user_input.split(":", 1)
            node_id = node_id.strip()
            message = message.strip()

            if node_id in network.nodes:
                # The CLI user sends a message to the node
                network.nodes[node_id].receive_message(message, "cli_user")
            else:
                print(f"No node with ID '{node_id}' found.")


# Modify the Flask app initialization
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes
network = None  # Will be set by the main function

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/tasks')
def show_tasks():
    global network
    if not network:
        return jsonify({"error": "Network not initialized"}), 500
    
    all_tasks = []
    for node_id, node in network.nodes.items():
        tasks = network.get_tasks_for_node(node_id)
        for task in tasks:
            all_tasks.append(task.to_dict())
    
    return jsonify(all_tasks)

@app.route('/nodes')
def show_nodes():
    global network
    if not network:
        return jsonify({"error": "Network not initialized"}), 500
    
    nodes = list(network.nodes.keys())
    return jsonify(nodes)

@app.route('/projects')
def show_projects():
    global network
    if not network:
        return jsonify({"error": "Network not initialized"}), 500
    
    all_projects = {}
    for node_id, node in network.nodes.items():
        for project_id, project in node.projects.items():
            if project_id not in all_projects:
                all_projects[project_id] = {
                    "name": project.get("name", ""),
                    "participants": list(project.get("participants", set())),
                    "owner": node_id
                }
    
    return jsonify(all_projects)

@app.route('/transcribe_audio', methods=['POST'])
def transcribe_audio():
    global network
    if not network:
        return jsonify({"error": "Network not initialized"}), 500
    
    data = request.json
    node_id = data.get('node_id')
    audio_data = data.get('audio_data')
    
    if not node_id or not audio_data:
        return jsonify({"error": "Missing node_id or audio_data"}), 400
    
    if node_id not in network.nodes:
        return jsonify({"error": f"Node {node_id} not found"}), 404
    
    # Decode the base64 audio data
    try:
        # Remove the data URL prefix if present
        if 'base64,' in audio_data:
            audio_data = audio_data.split('base64,')[1]
        
        audio_bytes = base64.b64decode(audio_data)
        
        # Save to a temporary file
        with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as temp_file:
            temp_file_path = temp_file.name
            temp_file.write(audio_bytes)
        
        # Transcribe using OpenAI's Whisper API
        with open(temp_file_path, 'rb') as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en"  # Specify English
            )
        
        # Clean up the temporary file
        os.unlink(temp_file_path)
        
        command_text = transcript.text
        
        # Use the same process as sending a text message
        response_collector = {"response": None, "terminal_output": []}
        
        # Override the print function temporarily to capture all output
        original_print = print
        
        def custom_print(text):
            if isinstance(text, str):
                # Capture all terminal output
                response_collector["terminal_output"].append(text)
                
                # Also capture the direct response
                if text.startswith(f"[{node_id}] Response: "):
                    response_collector["response"] = text.replace(f"[{node_id}] Response: ", "")
            original_print(text)
        
        # Replace print function
        import builtins
        builtins.print = custom_print
        
        try:
            # Send the message to the node
            network.nodes[node_id].receive_message(command_text, "cli_user")
            
            # Restore original print function
            builtins.print = original_print
            
            # Format terminal output for display
            terminal_text = "\n".join(response_collector["terminal_output"])
            
            # Generate speech from the response
            audio_response = None
            if response_collector["response"]:
                try:
                    speech_response = client.audio.speech.create(
                        model="tts-1",
                        voice="alloy",
                        input=response_collector["response"]
                    )
                    
                    # Convert to base64 for sending to the client
                    speech_response.stream_to_file("temp_speech.mp3")
                    with open("temp_speech.mp3", "rb") as audio_file:
                        audio_response = base64.b64encode(audio_file.read()).decode('utf-8')
                    os.unlink("temp_speech.mp3")
                except Exception as e:
                    print(f"Error generating speech: {str(e)}")
            
            return jsonify({
                "response": response_collector["response"],
                "terminal_output": terminal_text,
                "transcription": command_text,
                "audio_response": audio_response
            })
            
        except Exception as e:
            # Restore original print function
            builtins.print = original_print
            return jsonify({"error": str(e)}), 500
            
    except Exception as e:
        return jsonify({"error": f"Error processing audio: {str(e)}"}), 500

# Update the existing send_message route to use the common function
@app.route('/send_message', methods=['POST'])
def send_message():
    global network
    if not network:
        return jsonify({"error": "Network not initialized"}), 500
    
    data = request.json
    node_id = data.get('node_id')
    message = data.get('message')
    
    if not node_id or not message:
        return jsonify({"error": "Missing node_id or message"}), 400
    
    if node_id not in network.nodes:
        return jsonify({"error": f"Node {node_id} not found"}), 404
    
    return send_message_internal(node_id, message)

def start_flask():
    # Try different ports if 5000 is in use
    for port in range(5001, 5010):
        try:
            app.run(debug=False, host='0.0.0.0', port=port)
            break
        except OSError:
            print(f"Port {port} is in use, trying next port...")

def open_browser():
    # Wait a bit for Flask to start
    import time
    time.sleep(1.5)
    # Try different ports
    for port in range(5001, 5010):
        try:
            # Try to connect to check if this is the port being used
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('127.0.0.1', port))
            sock.close()
            if result == 0:  # Port is open, server is running here
                webbrowser.open(f'http://localhost:{port}')
                break
        except:
            continue

def demo_run():
    global network
    network = Network(log_file="communication_log.txt")

    # Create nodes
    ceo = LLMNode("ceo", knowledge="Knows entire org structure.")
    marketing = LLMNode("marketing", knowledge="Knows about markets.")
    engineering = LLMNode("engineering", knowledge="Knows codebase.")
    design = LLMNode("design", knowledge="Knows UI/UX best practices.")

    # Register them
    network.register_node(ceo)
    network.register_node(marketing)
    network.register_node(engineering)
    network.register_node(design)

    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=start_flask)
    flask_thread.daemon = True  # This ensures the thread will exit when the main program exits
    flask_thread.start()
    
    # Open browser automatically
    browser_thread = threading.Thread(target=open_browser)
    browser_thread.daemon = True
    browser_thread.start()

    # Start the CLI
    run_cli(network)


if __name__ == "__main__":
    demo_run()
