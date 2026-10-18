import asyncio
import aiohttp
import aiofiles
import logging
import argparse
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

class AsyncCPPPConnector:
    def __init__(self, base_url: str, output_dir: Path, max_concurrent: int = 10):
        self.base_url = base_url
        self.output_dir = output_dir
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def download_aoc(self, session: aiohttp.ClientSession, aoc_id: str, retry_count: int = 3) -> bool:
        """Downloads a single AOC document safely using streaming to prevent memory bloat."""
        url = f"{self.base_url}/tenders/aoc/{aoc_id}.pdf"
        file_path = self.output_dir / f"{aoc_id}.pdf"

        if file_path.exists() and file_path.stat().st_size > 0:
            return True

        async with self.semaphore:
            for attempt in range(retry_count):
                try:
                    async with session.get(url, timeout=30) as response:
                        if response.status == 200:
                            async with aiofiles.open(file_path, 'wb') as f:
                                async for chunk in response.content.iter_chunked(1024 * 64):
                                    await f.write(chunk)
                            return True
                        elif response.status in [429, 503, 504]:
                            wait_time = 2 ** attempt
                            logger.warning(f"Rate limited for {aoc_id}. Waiting {wait_time}s...")
                            await asyncio.sleep(wait_time)
                        else:
                            logger.error(f"Failed {aoc_id} with status {response.status}")
                            break
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout on {aoc_id}, attempt {attempt + 1}")
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Error downloading {aoc_id}: {str(e)}")
                    break
            
            return False

class BatchDownloader:
    def __init__(self, connector: AsyncCPPPConnector):
        self.connector = connector

    async def _worker(self, name: str, queue: asyncio.Queue, session: aiohttp.ClientSession):
        """Consumer worker that pulls AOC IDs from the queue and processes them."""
        while True:
            aoc_id = await queue.get()
            try:
                success = await self.connector.download_aoc(session, aoc_id)
            finally:
                queue.task_done()

    async def execute_batch(self, aoc_ids: list, num_workers: int = 20):
        """Sets up the connection pool and manages the worker queue."""
        queue = asyncio.Queue()
        
        for aoc_id in aoc_ids:
            queue.put_nowait(aoc_id)

        connector_args = aiohttp.TCPConnector(limit=50, keepalive_timeout=30)
        async with aiohttp.ClientSession(connector=connector_args) as session:
            workers = [
                asyncio.create_task(self._worker(f"worker-{i}", queue, session))
                for i in range(num_workers)
            ]

            await queue.join()

            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

async def main(inbox_dir: str, num_workers: int):
    output_path = Path(inbox_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Mock list of pending AOC IDs. In production, query from your database
    pending_aoc_ids = [f"AOC_2023_{str(i).zfill(3)}" for i in range(1, 100)] 
    
    connector = AsyncCPPPConnector(
        base_url="https://eprocure.gov.in/eprocure/app", 
        output_dir=output_path,
        max_concurrent=num_workers 
    )
    
    downloader = BatchDownloader(connector)
    logger.info(f"Starting batch download for {len(pending_aoc_ids)} items to {output_path}...")
    
    await downloader.execute_batch(pending_aoc_ids, num_workers=num_workers)
    logger.info("Batch acquisition complete. Files are ready for inbox processing.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="High-throughput CPPP AOC Downloader")
    parser.add_argument("--inbox", default="data/inbox", help="Target directory for downloaded PDFs")
    parser.add_argument("--workers", type=int, default=15, help="Number of concurrent download streams")
    args = parser.parse_args()
    
    asyncio.run(main(inbox_dir=args.inbox, num_workers=args.workers))
