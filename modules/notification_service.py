"""
Notification Service Module
Provides multi-channel notifications for stock analysis updates:
- Desktop notifications (macOS, Windows, Linux)

The daily push goes to Telegram (modules/telegram_notifier.py); email and
WeChat were retired on 2026-09-23.
"""

import logging
import platform
import subprocess
from datetime import datetime


logger = logging.getLogger(__name__)


class NotificationService:
    """
    Multi-channel notification service for stock analysis alerts
    """

    def __init__(self):
        self.platform = platform.system()

    def send_desktop_notification(self, title, message, sound=True):
        """
        Send native desktop notification

        Args:
            title (str): Notification title
            message (str): Notification message
            sound (bool): Whether to play sound

        Returns:
            bool: Success status
        """
        try:
            if self.platform == "Darwin":  # macOS
                return self._send_macos_notification(title, message, sound)
            elif self.platform == "Windows":
                return self._send_windows_notification(title, message)
            elif self.platform == "Linux":
                return self._send_linux_notification(title, message)
            else:
                logger.warning(f"Desktop notifications not supported on {self.platform}")
                return False

        except Exception as e:
            logger.error(f"Error sending desktop notification: {str(e)}")
            return False

    def _send_macos_notification(self, title, message, sound=True):
        """Send notification on macOS using native NSUserNotification API"""
        try:
            return self._send_macos_native(title, message, sound)
        except Exception:
            # Fallback to osascript if native API fails
            return self._send_macos_osascript(title, message, sound)

    def _send_macos_native(self, title, message, sound=True):
        """Send notification via macOS NSUserNotification (no 'Ned Deily' label)"""
        import objc
        NSUserNotification = objc.lookUpClass('NSUserNotification')
        NSUserNotificationCenter = objc.lookUpClass('NSUserNotificationCenter')

        notification = NSUserNotification.alloc().init()
        notification.setTitle_(title)
        notification.setInformativeText_(message)
        if sound:
            notification.setSoundName_('Glass')

        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        center.deliverNotification_(notification)
        logger.info("macOS native notification sent successfully")
        return True

    def _send_macos_osascript(self, title, message, sound=True):
        """Fallback: send notification via osascript"""
        try:
            title = title.replace('\\', '\\\\').replace('"', '\\"')
            message = message.replace('\\', '\\\\').replace('"', '\\"')

            script = f'display notification "{message}" with title "{title}"'
            if sound:
                script += ' sound name "Glass"'

            subprocess.run(
                ['osascript', '-e', script],
                capture_output=True, text=True, timeout=5
            )
            logger.info("macOS osascript notification sent successfully")
            return True

        except Exception as e:
            logger.error(f"Error sending macOS notification: {str(e)}")
            return False

    def _send_windows_notification(self, title, message):
        """Send notification on Windows using PowerShell"""
        try:
            # Escape quotes in message and title
            title = title.replace('"', '`"')
            message = message.replace('"', '`"')

            script = f'''
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
            [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
            [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

            $template = @"
            <toast>
                <visual>
                    <binding template="ToastText02">
                        <text id="1">{title}</text>
                        <text id="2">{message}</text>
                    </binding>
                </visual>
            </toast>
            "@

            $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
            $xml.LoadXml($template)
            $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Stock Analysis System").Show($toast)
            '''

            subprocess.run(['powershell', '-Command', script], check=True, capture_output=True)
            logger.info("Windows notification sent successfully")
            return True

        except Exception as e:
            logger.error(f"Error sending Windows notification: {str(e)}")
            return False

    def _send_linux_notification(self, title, message):
        """Send notification on Linux using notify-send"""
        try:
            subprocess.run(['notify-send', title, message], check=True, capture_output=True)
            logger.info("Linux notification sent successfully")
            return True

        except Exception as e:
            logger.error(f"Error sending Linux notification: {str(e)}")
            return False

    def send_analysis_complete_notification(self, signals_summary, portfolio_summary=None):
        """
        Send notification when daily analysis is complete

        Args:
            signals_summary (dict): Trading signals summary
            portfolio_summary (dict): Portfolio summary

        Returns:
            bool: Success status
        """
        try:
            # Prepare notification message
            buy_count = signals_summary.get('buy_signals', 0)
            sell_count = signals_summary.get('sell_signals', 0)
            top_buy = signals_summary.get('top_buy_picks', [])

            title = "📈 Stock Analysis Complete"

            message_parts = [
                f"Analysis finished at {datetime.now().strftime('%H:%M')}",
                f"",
                f"Signals: {buy_count} BUY, {sell_count} SELL"
            ]

            if top_buy:
                message_parts.append(f"Top picks: {', '.join(top_buy[:3])}")

            if portfolio_summary:
                daily_pnl = portfolio_summary.get('total_daily_pnl', 0)
                pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"
                message_parts.append(f"{pnl_emoji} Daily P&L: ${daily_pnl:,.0f}")

            message = "\n".join(message_parts)

            # Send desktop notification
            return self.send_desktop_notification(title, message)

        except Exception as e:
            logger.error(f"Error sending analysis complete notification: {str(e)}")
            return False
