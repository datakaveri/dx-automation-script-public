import psycopg2
from psycopg2 import sql
import json
import logging
from typing import Dict, Any
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DatabaseCleaner:
    """Handles transactional database cleanup with rollback support"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize database connection parameters
        
        Args:
            config: Dictionary containing connection details and record ID
        """
        self.config = config
        self.connection = None
    
    def connect(self) -> bool:
        """Establish database connection"""
        try:
            self.connection = psycopg2.connect(
                host=self.config['host'],
                port=self.config['port'],
                database=self.config['database'],
                user=self.config['databaseUser'],
                password=self.config['databasePassword']
            )
            logger.info("✓ Connected to database successfully")
            return True
        except psycopg2.Error as e:
            logger.error(f"✗ Connection failed: {e}")
            return False
    
    def cleanup_record(self) -> bool:
        """
        Execute all deletions in a single transaction
        Rolls back on any error
        
        Returns:
            True if successful, False otherwise
        """
        if not self.connection:
            logger.error("No database connection established")
            return False
        
        record_id = self.config['record_id']
        cursor = self.connection.cursor()
        
        try:
            # Start transaction (implicit in psycopg2)
            logger.info(f"Starting transaction for record: {record_id}")
            
            # Step 1: Delete from ri_details
            logger.info("Step 1: Deleting from ri_details...")
            cursor.execute(
                sql.SQL("DELETE FROM ri_details WHERE id = %s"),
                [record_id]
            )
            deleted_count = cursor.rowcount
            logger.info(f"  → Deleted {deleted_count} row(s) from ri_details")
            
            # Step 2: Delete from collections_enclosure
            logger.info("Step 2: Deleting from collections_enclosure...")
            cursor.execute(
                sql.SQL("DELETE FROM collections_enclosure WHERE collections_id = %s"),
                [record_id]
            )
            deleted_count = cursor.rowcount
            logger.info(f"  → Deleted {deleted_count} row(s) from collections_enclosure")
            
            # Step 3: Delete from collections_details
            logger.info("Step 3: Deleting from collections_details...")
            cursor.execute(
                sql.SQL("DELETE FROM collections_details WHERE id = %s"),
                [record_id]
            )
            deleted_count = cursor.rowcount
            logger.info(f"  → Deleted {deleted_count} row(s) from collections_details")
            
            # Step 4: Drop table if it exists
            logger.info("Step 4: Dropping table...")
            cursor.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(
                    sql.Identifier(record_id)
                )
            )
            logger.info("  → Table dropped successfully")
            
            # Commit all changes
            self.connection.commit()
            logger.info("✓ Transaction committed successfully")
            return True
            
        except psycopg2.Error as e:
            # Rollback on any error
            self.connection.rollback()
            logger.error(f"✗ Error during transaction: {e}")
            logger.error("✗ Transaction rolled back - all changes reverted")
            return False
        
        finally:
            cursor.close()
    
    def close(self):
        """Close database connection"""
        if self.connection:
            self.connection.close()
            logger.info("Database connection closed")




def load_config(config_file: str = None) -> Dict[str, Any]:
    """Load configuration from JSON file"""
    if config_file is None:
        # Use the directory where this script is located
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_file = os.path.join(script_dir, 'db_config.json')
    
    try:
        with open(config_file, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Config file '{config_file}' not found")
        logger.error(f"Looking for: {os.path.abspath(config_file)}")
        return None


def main():
    """Main execution"""
    # Load configuration
    config = load_config()
    
    if not config:
        logger.error("Failed to load configuration")
        return
    
    # Create cleaner and execute
    cleaner = DatabaseCleaner(config)
    
    try:
        if cleaner.connect():
            if cleaner.cleanup_record():
                logger.info("✓ Cleanup completed successfully")
            else:
                logger.error("✗ Cleanup failed")
        else:
            logger.error("✗ Failed to connect to database")
    
    finally:
        cleaner.close()


if __name__ == "__main__":
    main()