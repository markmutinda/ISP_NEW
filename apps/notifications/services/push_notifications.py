import logging
import json
import requests
from django.conf import settings
from django.utils import timezone
from typing import Dict, List, Optional, Tuple
from firebase_admin import messaging, initialize_app
from apps.core.models import AuditLog

logger = logging.getLogger(__name__)

class PushNotificationService:
    """Service for sending push notifications"""
    
    def __init__(self):
        self.config = getattr(settings, 'PUSH_NOTIFICATION_CONFIG', {})
        self.provider = self.config.get('provider', 'firebase')
        
        # Initialize Firebase if configured
        if self.provider == 'firebase' and not hasattr(self, 'firebase_app'):
            try:
                self.firebase_app = initialize_app()
            except:
                self.firebase_app = None
    
    def send_push(
        self,
        device_tokens: Union[str, List[str]],
        title: str,
        body: str,
        data: Optional[Dict] = None,
        image_url: Optional[str] = None,
        priority: str = 'normal',
        ttl: int = 2419200,  # 4 weeks in seconds
        metadata: Optional[Dict] = None
    ) -> Tuple[bool, str, Dict]:
        """
        Send push notification
        
        Returns: (success, message_id, response_data)
        """
        try:
            if isinstance(device_tokens, str):
                device_tokens = [device_tokens]
            
            if not device_tokens:
                return False, None, {"error": "No device tokens provided"}
            
            # Choose provider
            if self.provider == 'firebase':
                return self._send_via_firebase(
                    device_tokens=device_tokens,
                    title=title,
                    body=body,
                    data=data,
                    image_url=image_url,
                    priority=priority,
                    ttl=ttl,
                    metadata=metadata
                )
            elif self.provider == 'apns':
                return self._send_via_apns(
                    device_tokens=device_tokens,
                    title=title,
                    body=body,
                    data=data,
                    priority=priority,
                    metadata=metadata
                )
            else:
                logger.error(f"Unknown push provider: {self.provider}")
                return False, None, {"error": f"Unknown provider: {self.provider}"}
                
        except Exception as e:
            logger.error(f"Failed to send push notification: {str(e)}")
            return False, None, {"error": str(e)}
    
    def send_to_topic(
        self,
        topic: str,
        title: str,
        body: str,
        data: Optional[Dict] = None,
        image_url: Optional[str] = None
    ) -> Tuple[bool, str, Dict]:
        """Send push notification to a topic"""
        try:
            if self.provider == 'firebase':
                message = messaging.Message(
                    notification=messaging.Notification(
                        title=title,
                        body=body,
                        image=image_url
                    ),
                    data=data or {},
                    topic=topic
                )
                
                response = messaging.send(message)
                return True, response, {"topic": topic}
            else:
                return False, None, {"error": "Topics only supported for Firebase"}
                
        except Exception as e:
            logger.error(f"Failed to send to topic {topic}: {str(e)}")
            return False, None, {"error": str(e)}
    
    def subscribe_to_topic(
        self,
        device_tokens: List[str],
        topic: str
    ) -> Tuple[bool, Dict]:
        """Subscribe devices to a topic"""
        try:
            if self.provider == 'firebase':
                response = messaging.subscribe_to_topic(device_tokens, topic)
                return True, {
                    'success_count': response.success_count,
                    'failure_count': response.failure_count,
                    'errors': [
                        {'index': err.index, 'reason': err.reason}
                        for err in response.errors
                    ] if response.errors else []
                }
            else:
                return False, {"error": "Topics only supported for Firebase"}
                
        except Exception as e:
            logger.error(f"Failed to subscribe to topic {topic}: {str(e)}")
            return False, {"error": str(e)}
    
    def _send_via_firebase(
        self,
        device_tokens: List[str],
        title: str,
        body: str,
        data: Optional[Dict],
        image_url: Optional[str],
        priority: str,
        ttl: int,
        metadata: Optional[Dict]
    ) -> Tuple[bool, str, Dict]:
        """Send via Firebase Cloud Messaging"""
        try:
            # Create notification
            notification = messaging.Notification(
                title=title,
                body=body,
                image=image_url
            )
            
            # Android config
            android_config = messaging.AndroidConfig(
                priority='high' if priority in ['high', 'urgent'] else 'normal',
                ttl=timezone.timedelta(seconds=ttl),
                notification=messaging.AndroidNotification(
                    icon='notification_icon',
                    color='#FF5722',
                    sound='default'
                )
            )
            
            # APNS config (for iOS)
            apns_config = messaging.APNSConfig(
                headers={
                    'apns-priority': '10' if priority in ['high', 'urgent'] else '5'
                },
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        alert=messaging.ApsAlert(
                            title=title,
                            body=body
                        ),
                        sound='default',
                        badge=1
                    )
                )
            )
            
            # Create message
            message = messaging.MulticastMessage(
                notification=notification,
                data=data or {},
                tokens=device_tokens,
                android=android_config,
                apns=apns_config
            )
            
            # Send
            response = messaging.send_multicast(message)
            
            # Process results
            success_count = response.success_count
            failure_count = response.failure_count
            
            # Get individual results
            results = []
            for idx, result in enumerate(response.responses):
                if result.success:
                    results.append({
                        'token': device_tokens[idx],
                        'success': True,
                        'message_id': result.message_id
                    })
                else:
                    results.append({
                        'token': device_tokens[idx],
                        'success': False,
                        'error': result.exception
                    })
            
            # Log
            self._log_push_send(
                tokens=device_tokens,
                title=title,
                success=success_count > 0,
                results=results,
                metadata=metadata
            )
            
            # Generate a combined message ID
            import uuid
            message_id = str(uuid.uuid4())
            
            response_data = {
                'success_count': success_count,
                'failure_count': failure_count,
                'results': results,
                'provider': 'firebase'
            }
            
            return success_count > 0, message_id, response_data
            
        except Exception as e:
            logger.error(f"Firebase send failed: {str(e)}")
            return False, None, {"error": str(e)}
    
    def _send_via_apns(
        self,
        device_tokens: List[str],
        title: str,
        body: str,
        data: Optional[Dict],
        priority: str,
        metadata: Optional[Dict]
    ) -> Tuple[bool, str, Dict]:
        """Send via Apple Push Notification Service"""
        try:
            import jwt
            import time
            
            # APNS configuration
            team_id = self.config.get('apns_team_id')
            key_id = self.config.get('apns_key_id')
            auth_key = self.config.get('apns_auth_key')
            bundle_id = self.config.get('apns_bundle_id')
            
            if not all([team_id, key_id, auth_key, bundle_id]):
                return False, None, {"error": "APNS configuration missing"}
            
            # Generate JWT token
            token = jwt.encode(
                {
                    'iss': team_id,
                    'iat': time.time()
                },
                auth_key,
                algorithm='ES256',
                headers={'kid': key_id}
            )
            
            # Prepare notification payload
            payload = {
                'aps': {
                    'alert': {
                        'title': title,
                        'body': body
                    },
                    'sound': 'default',
                    'badge': 1
                }
            }
            
            # Add custom data
            if data:
                payload.update(data)
            
            # Send to each device
            headers = {
                'authorization': f'bearer {token}',
                'apns-topic': bundle_id,
                'apns-priority': '10' if priority in ['high', 'urgent'] else '5'
            }
            
            results = []
            success_count = 0
            failure_count = 0
            
            for token in device_tokens:
                try:
                    # Use production or sandbox endpoint based on environment
                    endpoint = 'https://api.push.apple.com'  # Production
                    if settings.DEBUG:
                        endpoint = 'https://api.sandbox.push.apple.com'
                    
                    response = requests.post(
                        f'{endpoint}/3/device/{token}',
                        headers=headers,
                        json=payload,
                        timeout=10
                    )
                    
                    if response.status_code == 200:
                        success_count += 1
                        results.append({
                            'token': token,
                            'success': True
                        })
                    else:
                        failure_count += 1
                        results.append({
                            'token': token,
                            'success': False,
                            'error': response.text
                        })
                        
                except Exception as e:
                    failure_count += 1
                    results.append({
                        'token': token,
                        'success': False,
                        'error': str(e)
                    })
            
            # Log
            self._log_push_send(
                tokens=device_tokens,
                title=title,
                success=success_count > 0,
                results=results,
                metadata=metadata
            )
            
            # Generate message ID
            import uuid
            message_id = str(uuid.uuid4())
            
            response_data = {
                'success_count': success_count,
                'failure_count': failure_count,
                'results': results,
                'provider': 'apns'
            }
            
            return success_count > 0, message_id, response_data
            
        except Exception as e:
            logger.error(f"APNS send failed: {str(e)}")
            return False, None, {"error": str(e)}
    
    def _log_push_send(
        self,
        tokens: List[str],
        title: str,
        success: bool,
        results: List[Dict],
        metadata: Optional[Dict]
    ):
        """Log push notification attempt"""
        from apps.core.models import AuditLog
        
        AuditLog.objects.create(
            user=None,
            action='PUSH_NOTIFICATION_SENT',
            ip_address='127.0.0.1',
            details={
                'tokens_count': len(tokens),
                'title': title,
                'success': success,
                'results': results,
                'provider': self.provider,
                'metadata': metadata or {}
            }
        )