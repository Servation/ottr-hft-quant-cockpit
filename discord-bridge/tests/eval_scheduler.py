import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.scheduler import meeting_scheduler


async def run_scheduler_test():
    print("Testing APScheduler Dual-System Logic...")

    # 1. Start the scheduler (initializes the cron + interval jobs)
    await meeting_scheduler.start(bot=None)

    def soonest_job():
        jobs = meeting_scheduler._scheduler.get_jobs()
        return min(jobs, key=lambda j: j.next_run_time) if jobs else None

    # 2. Inspect the default next meeting
    before = soonest_job()
    next_type, next_time = meeting_scheduler.get_next_meeting_info()
    print(f"\n[Cron Default] Next scheduled meeting: {next_type} at {next_time}")
    print(f"  (soonest job id: {before.id if before else None})")

    # 3. Simulate an agent scheduling a dynamic meeting 10 minutes out
    print("\nSimulating Agent calling `schedule_dynamic_meeting(10)`...")
    meeting_scheduler.schedule_dynamic_meeting(minutes=10)

    # 4. Inspect again
    after = soonest_job()
    next_type_dyn, next_time_dyn = meeting_scheduler.get_next_meeting_info()
    print(f"[Dynamic Update] Next scheduled meeting is now: {next_type_dyn} at {next_time_dyn}")
    print(f"  (soonest job id: {after.id if after else None})")

    # 5. Validate against the ACTUAL scheduler state, not formatted time strings
    #    (the old eval compared a PDT-formatted string to a UTC-formatted one,
    #    which "passed" purely because of the timezone-suffix difference).
    expected_fire = datetime.now(timezone.utc) + timedelta(minutes=10)
    is_dynamic = after is not None and after.id.startswith("dynamic_meeting_")
    fires_sooner = (
        before is not None and after is not None
        and after.next_run_time < before.next_run_time
    )
    close_to_10min = (
        after is not None
        and abs((after.next_run_time - expected_fire).total_seconds()) < 60
    )

    if is_dynamic and fires_sooner and close_to_10min:
        print(
            "\n✅ EVALUATION PASSED: The dynamic meeting became the next-to-fire job, "
            "scheduled ~10 minutes out, ahead of the standard cron/interval schedule."
        )
    else:
        print(
            "\n❌ EVALUATION FAILED: dynamic meeting did not take priority correctly "
            f"(is_dynamic={is_dynamic}, fires_sooner={fires_sooner}, close_to_10min={close_to_10min})."
        )

    await meeting_scheduler.stop()


if __name__ == "__main__":
    asyncio.run(run_scheduler_test())
