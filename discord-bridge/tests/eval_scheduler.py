import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.scheduler import meeting_scheduler

async def run_scheduler_test():
    print("Testing APScheduler Dual-System Logic...")
    
    # 1. Start the scheduler (this initializes the 4-hour cron jobs)
    # We pass None for the bot parameter since we don't need discord for this unit test
    await meeting_scheduler.start(bot=None)
    
    # 2. Check the upcoming scheduled meeting
    next_type, next_time = meeting_scheduler.get_next_meeting_info()
    print(f"\n[Cron Default] Next scheduled meeting: {next_type} at {next_time}")
    
    # 3. Simulate an Agent scheduling a dynamic meeting (e.g., "reconvene in 10 minutes")
    print("\nSimulating Agent calling `schedule_dynamic_meeting(10)`...")
    meeting_scheduler.schedule_dynamic_meeting(minutes=10)
    
    # 4. Check the queue again
    next_type_dyn, next_time_dyn = meeting_scheduler.get_next_meeting_info()
    print(f"[Dynamic Update] Next scheduled meeting is now: {next_type_dyn} at {next_time_dyn}")
    
    if "Dynamic meeting" in next_time_dyn or next_time_dyn != next_time:
         print("\n✅ EVALUATION PASSED: The scheduler successfully ingested the agent's dynamic meeting and prioritized it over the standard cron.")
    else:
         print("\n❌ EVALUATION FAILED: The agent's dynamic meeting did not override the default cron schedule.")
    
    await meeting_scheduler.stop()

if __name__ == "__main__":
    asyncio.run(run_scheduler_test())
