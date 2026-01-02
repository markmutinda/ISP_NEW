import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from django.conf import settings
from django.core.mail import send_mail as django_send_mail
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from typing import Dict, List, Optional, Tuple, Union
import threading

logger = logging.getLogger(__name__)

class EmailService:
    """Service for sending email notifications"""
    
    def __init__(self):
        self.config = getattr(settings, 'EMAIL_CONFIG', {})
        self.default_from = self.config.get('default_from', settings.DEFAULT_FROM_EMAIL)
        self.backend = self.config.get('backend', 'django')
        self.bcc_enabled = self.config.get('bcc_enabled', True)
        self.bcc_email = self.config.get('bcc_email')
        
        # Initialize SMTP if not using Django's backend
        if self.backend == 'smtp_direct':
            self.smtp_host = self.config.get('smtp_host', 'localhost')
            self.smtp_port = self.config.get('smtp_port', 587)
            self.smtp_username = self.config.get('smtp_username')
            self.smtp_password = self.config.get('smtp_password')
            self.use_tls = self.config.get('use_tls', True)
    
    def send_email(
        self,
        recipient: Union[str, List[str]],
        subject: str,
        message: str,
        html_message: Optional[str] = None,
        from_email: Optional[str] = None,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        attachments: Optional[List[Dict]] = None,
        priority: str = 'normal',
        metadata: Optional[Dict] = None
    ) -> Tuple[bool, str, Dict]:
        """
        Send email to recipient(s)
        
        Returns: (success, message_id, response_data)
        """
        try:
            # Prepare recipients
            if isinstance(recipient, str):
                recipients = [recipient]
            else:
                recipients = recipient
            
            # Validate recipients
            valid_recipients = []
            for email in recipients:
                if self._validate_email(email):
                    valid_recipients.append(email)
            
            if not valid_recipients:
                return False, None, {"error": "No valid recipients"}
            
            # Prepare BCC
            final_bcc = []
            if self.bcc_enabled and self.bcc_email:
                final_bcc.append(self.bcc_email)
            if bcc:
                final_bcc.extend(bcc)
            
            # Prepare from email
            sender = from_email or self.default_from
            
            # Prepare message
            if html_message:
                text_message = strip_tags(html_message)
            else:
                text_message = message
                html_message = self._wrap_in_html_template(message, subject, metadata)
            
            # Add BCC for tracking
            if metadata and 'notification_id' in metadata:
                tracking_bcc = f"track-{metadata['notification_id']}@tracking.{settings.DOMAIN}"
                final_bcc.append(tracking_bcc)
            
            # Send based on backend
            if self.backend == 'django':
                success, message_id = self._send_via_django(
                    subject=subject,
                    message=text_message,
                    html_message=html_message,
                    from_email=sender,
                    recipients=valid_recipients,
                    cc=cc,
                    bcc=final_bcc,
                    attachments=attachments
                )
            elif self.backend == 'smtp_direct':
                success, message_id = self._send_via_smtp(
                    subject=subject,
                    text_message=text_message,
                    html_message=html_message,
                    from_email=sender,
                    recipients=valid_recipients,
                    cc=cc,
                    bcc=final_bcc,
                    attachments=attachments
                )
            else:
                # Default to Django
                success, message_id = self._send_via_django(
                    subject=subject,
                    message=text_message,
                    html_message=html_message,
                    from_email=sender,
                    recipients=valid_recipients,
                    cc=cc,
                    bcc=final_bcc,
                    attachments=attachments
                )
            
            # Log the attempt
            self._log_email_send(
                recipients=valid_recipients,
                subject=subject,
                success=success,
                message_id=message_id,
                metadata=metadata
            )
            
            response_data = {
                'recipients': valid_recipients,
                'backend': self.backend,
                'timestamp': str(settings.timezone.now())
            }
            
            return success, message_id, response_data
            
        except Exception as e:
            logger.error(f"Failed to send email: {str(e)}")
            return False, None, {"error": str(e)}
    
    def send_template_email(
        self,
        recipient: Union[str, List[str]],
        template_name: str,
        context: Dict,
        from_email: Optional[str] = None,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        attachments: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None
    ) -> Tuple[bool, str, Dict]:
        """Send email using a template"""
        try:
            # Load template
            html_content = render_to_string(
                f'emails/{template_name}.html',
                context
            )
            
            # Extract subject from template if available
            subject = context.get('subject', 'No Subject')
            
            # Send email
            return self.send_email(
                recipient=recipient,
                subject=subject,
                message='',  # Will be extracted from HTML
                html_message=html_content,
                from_email=from_email,
                cc=cc,
                bcc=bcc,
                attachments=attachments,
                metadata=metadata
            )
            
        except Exception as e:
            logger.error(f"Failed to send template email: {str(e)}")
            return False, None, {"error": str(e)}
    
    def send_bulk_emails(
        self,
        recipients: List[Dict],
        template_name: str,
        common_context: Dict,
        batch_size: int = 50,
        delay_between_batches: int = 1
    ) -> Dict:
        """Send bulk emails with individual contexts"""
        from django.utils import timezone
        import time
        
        results = {
            'total': len(recipients),
            'success': 0,
            'failed': 0,
            'started_at': timezone.now(),
            'details': []
        }
        
        # Process in batches
        for i in range(0, len(recipients), batch_size):
            batch = recipients[i:i + batch_size]
            
            threads = []
            for recipient_data in batch:
                email = recipient_data['email']
                context = {**common_context, **recipient_data.get('context', {})}
                
                # Create thread for each email
                thread = threading.Thread(
                    target=self._send_single_email_thread,
                    args=(email, template_name, context, results)
                )
                threads.append(thread)
                thread.start()
            
            # Wait for all threads in batch to complete
            for thread in threads:
                thread.join()
            
            # Delay between batches
            if i + batch_size < len(recipients):
                time.sleep(delay_between_batches)
        
        results['completed_at'] = timezone.now()
        return results
    
    def _send_single_email_thread(self, email, template_name, context, results):
        """Thread function for sending single email"""
        try:
            success, message_id, response = self.send_template_email(
                recipient=email,
                template_name=template_name,
                context=context
            )
            
            if success:
                results['success'] += 1
            else:
                results['failed'] += 1
            
            results['details'].append({
                'email': email,
                'success': success,
                'message_id': message_id,
                'error': response.get('error') if not success else None
            })
            
        except Exception as e:
            results['failed'] += 1
            results['details'].append({
                'email': email,
                'success': False,
                'message_id': None,
                'error': str(e)
            })
    
    def _send_via_django(
        self,
        subject: str,
        message: str,
        html_message: str,
        from_email: str,
        recipients: List[str],
        cc: List[str],
        bcc: List[str],
        attachments: List[Dict]
    ) -> Tuple[bool, str]:
        """Send email using Django's email backend"""
        import uuid
        
        try:
            email = EmailMultiAlternatives(
                subject=subject,
                body=message,
                from_email=from_email,
                to=recipients,
                cc=cc,
                bcc=bcc
            )
            
            # Add HTML alternative
            email.attach_alternative(html_message, "text/html")
            
            # Add attachments
            if attachments:
                for attachment in attachments:
                    if isinstance(attachment, dict):
                        email.attach(
                            filename=attachment.get('filename', 'attachment'),
                            content=attachment.get('content'),
                            mimetype=attachment.get('mimetype')
                        )
            
            # Send
            email.send(fail_silently=False)
            
            # Generate message ID
            message_id = str(uuid.uuid4())
            
            return True, message_id
            
        except Exception as e:
            logger.error(f"Django email send failed: {str(e)}")
            return False, None
    
    def _send_via_smtp(
        self,
        subject: str,
        text_message: str,
        html_message: str,
        from_email: str,
        recipients: List[str],
        cc: List[str],
        bcc: List[str],
        attachments: List[Dict]
    ) -> Tuple[bool, str]:
        """Send email directly via SMTP"""
        import uuid
        
        try:
            # Create message
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = from_email
            msg['To'] = ', '.join(recipients)
            if cc:
                msg['Cc'] = ', '.join(cc)
            if bcc:
                msg['Bcc'] = ', '.join(bcc)
            
            # Add text part
            text_part = MIMEText(text_message, 'plain')
            msg.attach(text_part)
            
            # Add HTML part
            html_part = MIMEText(html_message, 'html')
            msg.attach(html_part)
            
            # Add attachments
            if attachments:
                for attachment in attachments:
                    if isinstance(attachment, dict):
                        attach_part = MIMEApplication(
                            attachment.get('content'),
                            Name=attachment.get('filename')
                        )
                        attach_part['Content-Disposition'] = f'attachment; filename="{attachment.get("filename")}"'
                        msg.attach(attach_part)
            
            # Connect to SMTP server and send
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                
                if self.smtp_username and self.smtp_password:
                    server.login(self.smtp_username, self.smtp_password)
                
                # Send to all recipients
                all_recipients = recipients + (cc or []) + (bcc or [])
                server.send_message(msg, from_email, all_recipients)
            
            # Generate message ID
            message_id = str(uuid.uuid4())
            
            return True, message_id
            
        except Exception as e:
            logger.error(f"SMTP email send failed: {str(e)}")
            return False, None
    
    def _validate_email(self, email: str) -> bool:
        """Validate email address format"""
        import re
        
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return bool(re.match(pattern, email))
    
    def _wrap_in_html_template(self, message: str, subject: str, metadata: Optional[Dict]) -> str:
        """Wrap plain text message in HTML template"""
        try:
            context = {
                'content': message,
                'subject': subject,
                'metadata': metadata or {},
                'year': settings.timezone.now().year,
                'company_name': getattr(settings, 'COMPANY_NAME', 'ISP Management System')
            }
            
            return render_to_string('emails/base_template.html', context)
        except:
            # Fallback minimal HTML
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <title>{subject}</title>
                <style>
                    body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                    .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                    .content {{ background: #f9f9f9; padding: 20px; border-radius: 5px; }}
                    .footer {{ margin-top: 20px; font-size: 12px; color: #666; text-align: center; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="content">
                        {message.replace('\\n', '<br>')}
                    </div>
                    <div class="footer">
                        Sent from {getattr(settings, 'COMPANY_NAME', 'ISP Management System')}
                    </div>
                </div>
            </body>
            </html>
            """
    
    def _log_email_send(
        self,
        recipients: List[str],
        subject: str,
        success: bool,
        message_id: Optional[str],
        metadata: Optional[Dict]
    ):
        """Log email sending attempt"""
        from apps.core.models import AuditLog
        
        AuditLog.objects.create(
            user=None,  # System action
            action='EMAIL_SENT',
            ip_address='127.0.0.1',
            details={
                'recipients': recipients,
                'subject': subject,
                'success': success,
                'message_id': message_id,
                'backend': self.backend,
                'metadata': metadata or {}
            }
        )