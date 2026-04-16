from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean
from database import Base # CRITICAL FIX: Use the shared Base from database.py
import datetime

class ChatSession(Base):
    """Production-grade model for storing AI sales funnel state."""
    __tablename__ = "chat_sessions"
    
    id = Column(Integer, primary_key=True, index=True)
    whatsapp_chat_id = Column(String, unique=True, index=True, nullable=False)
    history_json = Column(JSON, default=[]) 
    
    # AI State Flags
    is_qualified = Column(Boolean, default=False)
    needs_human = Column(Boolean, default=False)
    crm_lead_id = Column(String, nullable=True) 
    
    # Booking & Funnel Progression
    booked_at = Column(DateTime, nullable=True)
    booked_date = Column(String, nullable=True) 
    
    # Proactive Engine Markers
    is_paid = Column(Boolean, default=False)
    is_reminder_sent = Column(Boolean, default=False)
    is_feedback_sent = Column(Boolean, default=False)
    followup_count = Column(Integer, default=0)
    
    # CRM Profile Data
    client_name = Column(String, nullable=True)
    child_age = Column(Integer, nullable=True)
    client_intent = Column(String, nullable=True)
    client_city = Column(String, nullable=True)
    client_audience = Column(String, nullable=True)  # "children" | "adults"
    preferred_time = Column(String, nullable=True)
    client_format = Column(String, nullable=True)  # "online" | "offline"
    funnel_stage = Column(String, default="new")  # new|name|city|qualified|booked|rescheduled|declined|paid
    is_subscription_offered = Column(Boolean, default=False)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_interaction = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
