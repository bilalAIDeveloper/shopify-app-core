
import asyncio
import json
import sys

# Add the project root to the python path so we can import app modules
import os
sys.path.append(os.path.join(os.path.dirname(__file__)))

from app.database.engine import SessionLocal
from app.database.repositories.shop_installation_repository import ShopInstallationRepository
from app.services.shopify_service import ShopifyService
from app.database.models import ShopInstallation

async def main():
    # 1. Create a DB session
    db = SessionLocal()
    
    try:
        # 2. Get the first shop installation from the DB
        # Access the database directly since get_all() might not exist on the repo
        first_install = db.query(ShopInstallation).first()
        
        if not first_install:
            print("No active shop installations found in the database.")
            return

        shop_domain = first_install.shop_domain
        token = first_install.access_token
        
        print(f"\n--- Found Shop in DB: {shop_domain} ---")
        print(f"Token (masked): {token[:4]}...{token[-4:]}\n")

        # 3. Call Shopify API to get all shop details
        service = ShopifyService(shop_domain=shop_domain, access_token=token)
        print("Fetching shop details from Shopify API...\n")
        
        shop_details = await service.get_shop_info()
        
        # 4. Display all details nicely
        print(json.dumps(shop_details, indent=4, default=str))
        
        print(f"\nSuccessfully retrieved {len(shop_details)} fields for {shop_domain}")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
