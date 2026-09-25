import requests
import json
import logging
import sys
import time
from typing import Dict, List, Optional, Any
from pathlib import Path
from datetime import datetime
import os


# ============================================================
# LOGGING SETUP
# ============================================================

class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors"""
    
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[92m',       # Green
        'WARNING': '\033[93m',    # Yellow
        'ERROR': '\033[91m',      # Red
        'CRITICAL': '\033[95m',   # Magenta
        'RESET': '\033[0m'
    }
    
    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        record.msg = f"{log_color}{record.msg}{self.COLORS['RESET']}"
        return super().format(record)


def setup_logging(log_file: str = None) -> logging.Logger:
    """Configure logging with both file and console output"""
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = ColoredFormatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # File handler
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
    
    return logger


# ============================================================
# CONFIGURATION MANAGEMENT
# ============================================================

class Config:
    """Configuration management"""
    
    def __init__(self, config_file: str = 'config.json'):
        self.config_file = config_file
        self.data = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, self.config_file)
        
        if not os.path.exists(config_path):
            raise FileNotFoundError(
                f"Configuration file not found: {config_path}\n"
                "Please create config.json with required settings."
            )
        
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {self.config_file}: {e}")
    
    def get(self, key: str, default=None) -> Any:
        """Get configuration value"""
        return self.data.get(key, default)
    
    def validate(self) -> bool:
        """Validate required configuration"""
        required = ['api_base', 'token', 'collection_id']
        missing = [k for k in required if not self.get(k)]
        
        if missing:
            raise ValueError(f"Missing required config: {', '.join(missing)}")
        
        return True


# ============================================================
# STAC DELETION CLIENT
# ============================================================

class STACDeletionClient:
    """STAC Collection item deletion client with retry logic"""
    
    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.session = requests.Session()
        self._setup_session()
        self.stats = {
            'total_deleted': 0,
            'total_failed': 0,
            'total_skipped': 0,
            'batches_processed': 0
        }
    
    def _setup_session(self):
        """Setup requests session with headers"""
        self.session.headers.update({
            'Authorization': f"Bearer {self.config.get('token')}",
            'Accept': 'application/json'
        })
    
    def get_collection_items(
        self,
        offset: int = 0,
        limit: int = 100,
        retries: int = 3
    ) -> Optional[List[Dict]]:
        """Fetch collection items with retry logic"""
        
        url = (
            f"{self.config.get('api_base')}/"
            f"{self.config.get('collection_id')}/items"
            f"?offset={offset}&limit={limit}"
        )
        
        for attempt in range(1, retries + 1):
            try:
                self.logger.debug(f"Fetching items (attempt {attempt}/{retries}): {url}")
                
                response = self.session.get(
                    url,
                    timeout=self.config.get('request_timeout', 60)
                )
                
                if response.ok:
                    data = response.json()
                    items = data.get('features', [])
                    self.logger.info(f"✓ Fetched {len(items)} items from offset {offset}")
                    return items
                
                elif response.status_code == 401:
                    self.logger.error("Authentication failed - Invalid or expired token")
                    return None
                
                elif response.status_code == 404:
                    self.logger.warning("Collection not found")
                    return None
                
                else:
                    self.logger.warning(
                        f"Fetch failed (HTTP {response.status_code}, "
                        f"attempt {attempt}/{retries})"
                    )
                    if attempt < retries:
                        wait_time = 2 ** attempt  # Exponential backoff
                        self.logger.debug(f"Retrying in {wait_time}s...")
                        time.sleep(wait_time)
            
            except requests.exceptions.Timeout:
                self.logger.warning(f"Request timeout (attempt {attempt}/{retries})")
                if attempt < retries:
                    time.sleep(2 ** attempt)
            
            except requests.exceptions.RequestException as e:
                self.logger.error(f"Request failed: {e} (attempt {attempt}/{retries})")
                if attempt < retries:
                    time.sleep(2 ** attempt)
        
        self.logger.error("Failed to fetch items after all retries")
        return None
    
    def delete_item(
        self,
        item_id: str,
        retries: int = 2
    ) -> bool:
        """Delete a single item with retry logic"""
        
        url = (
            f"{self.config.get('api_base')}/"
            f"{self.config.get('collection_id')}/items/"
            f"{item_id}"
        )
        
        for attempt in range(1, retries + 1):
            try:
                response = self.session.delete(
                    url,
                    timeout=self.config.get('request_timeout', 60)
                )
                
                if response.ok:
                    self.logger.debug(f"[DELETED] {item_id} | HTTP {response.status_code}")
                    return True
                
                elif response.status_code == 404:
                    self.logger.warning(f"[NOT FOUND] {item_id}")
                    return False
                
                elif response.status_code == 401:
                    self.logger.error(f"[AUTH ERROR] {item_id}")
                    return False
                
                else:
                    self.logger.warning(
                        f"[FAILED] {item_id} | HTTP {response.status_code} "
                        f"(attempt {attempt}/{retries})"
                    )
                    if attempt < retries:
                        time.sleep(1)
            
            except requests.exceptions.RequestException as e:
                self.logger.error(f"[ERROR] {item_id} | {e} (attempt {attempt}/{retries})")
                if attempt < retries:
                    time.sleep(1)
        
        return False
    
    def delete_collection_items(self) -> Dict[str, int]:
        """Delete all items in collection with batch processing"""
        
        batch_number = 1
        batch_size = self.config.get('batch_size', 100)
        delete_delay = self.config.get('delete_delay', 0.2)
        
        self.logger.info("=" * 60)
        self.logger.info("STAC COLLECTION ITEM DELETION")
        self.logger.info("=" * 60)
        self.logger.info(f"Collection ID : {self.config.get('collection_id')}")
        self.logger.info(f"Batch size    : {batch_size}")
        self.logger.info(f"Delete delay  : {delete_delay}s")
        self.logger.info("=" * 60)
        
        while True:
            self.logger.info(f"\n{'=' * 20} BATCH {batch_number} {'=' * 20}")
            
            # Always use offset=0 to fetch remaining items
            items = self.get_collection_items(
                offset=0,
                limit=batch_size
            )
            
            if items is None:
                self.logger.error("Stopping - item retrieval failed")
                break
            
            if not items:
                self.logger.info("✓ No more items found - deletion complete")
                break
            
            self.logger.info(f"Found {len(items)} items in this batch")
            
            batch_deleted = 0
            batch_failed = 0
            batch_skipped = 0
            
            for idx, feature in enumerate(items, 1):
                item_id = feature.get('id')
                
                if not item_id:
                    self.logger.warning(f"[{idx}/{len(items)}] [SKIPPED] Item without ID")
                    batch_skipped += 1
                    self.stats['total_skipped'] += 1
                    continue
                
                self.logger.info(f"[{idx}/{len(items)}] Deleting: {item_id}")
                
                if self.delete_item(item_id):
                    batch_deleted += 1
                    self.stats['total_deleted'] += 1
                else:
                    batch_failed += 1
                    self.stats['total_failed'] += 1
                
                time.sleep(delete_delay)
            
            # Batch summary
            self.logger.info(f"\nBatch {batch_number} Summary:")
            self.logger.info(f"  Deleted : {batch_deleted}")
            self.logger.info(f"  Failed  : {batch_failed}")
            self.logger.info(f"  Skipped : {batch_skipped}")
            
            self.stats['batches_processed'] += 1
            
            # Stop if nothing was deleted (prevent infinite loop)
            if batch_deleted == 0:
                self.logger.warning(
                    "No items were deleted in this batch - stopping to prevent infinite loop"
                )
                break
            
            batch_number += 1
        
        return self._print_summary()
    
    def _print_summary(self) -> Dict[str, int]:
        """Print final summary"""
        self.logger.info("\n" + "=" * 60)
        self.logger.info("DELETION COMPLETED")
        self.logger.info("=" * 60)
        self.logger.info(f"Collection ID     : {self.config.get('collection_id')}")
        self.logger.info(f"Batches processed : {self.stats['batches_processed']}")
        self.logger.info(f"Total deleted     : {self.stats['total_deleted']}")
        self.logger.info(f"Total failed      : {self.stats['total_failed']}")
        self.logger.info(f"Total skipped     : {self.stats['total_skipped']}")
        self.logger.info("=" * 60)
        
        return self.stats
    
    def close(self):
        """Close session"""
        self.session.close()
        self.logger.debug("Session closed")


# ============================================================
# MAIN
# ============================================================

def main():
    """Main execution"""
    
    # Setup logging
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"stac_deletion_{timestamp}.log"
    logger = setup_logging(log_file)
    
    logger.info(f"Log file: {log_file}")
    
    try:
        # Load configuration
        logger.info("Loading configuration...")
        config = Config('config.json')
        config.validate()
        logger.info("✓ Configuration loaded successfully")
        
        # Create client and execute
        client = STACDeletionClient(config, logger)
        stats = client.delete_collection_items()
        client.close()
        
        # Exit with appropriate code
        exit_code = 0 if stats['total_failed'] == 0 else 1
        sys.exit(exit_code)
    
    except FileNotFoundError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()