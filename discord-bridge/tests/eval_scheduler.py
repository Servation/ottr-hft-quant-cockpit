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

    # 5. Validate against the ACTUAL scheduler state, not formatted time strings.
    #    Assert the dynamic meeting is registered to fire ~10 minutes out. We do NOT
    #    assert it's the globally-soonest job: that's wall-clock dependent — a cron
    #    meeting or interval job can legitimately be <10 min away (e.g. running this
    #    just before the top of a meeting hour), which used to make the eval flaky.
    expected_fire = datetime.now(timezone.utc) + timedelta(minutes=10)
    jobs = meeting_scheduler._scheduler.get_jobs()
    dynamic_jobs = [j for j in jobs if j.id.startswith("dynamic_meeting_")]
    is_dynamic = len(dynamic_jobs) == 1
    close_to_10min = (
        is_dynamic
        and abs((dynamic_jobs[0].next_run_time - expected_fire).total_seconds()) < 60
    )

    if is_dynamic and close_to_10min:
        print(
            "\n✅ EVALUATION PASSED: schedule_dynamic_meeting(10) registered a dynamic "
            "meeting firing ~10 minutes out."
        )
    else:
        print(
            "\n❌ EVALUATION FAILED: dynamic meeting not registered correctly "
            f"(is_dynamic={is_dynamic}, close_to_10min={close_to_10min})."
        )

    await meeting_scheduler.stop()


if __name__ == "__main__":
    asyncio.run(run_scheduler_test())
