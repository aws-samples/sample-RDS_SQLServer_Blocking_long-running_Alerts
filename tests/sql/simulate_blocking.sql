-- simulate_blocking.sql
-- Reproduce a blocking chain so the root-blocker CTE can identify it.
-- Open TWO SSMS sessions and run the two blocks separately.
-- Run against a NON-production user database.

-- ============================================================
-- Session 1 (Blocker): acquire exclusive locks, DO NOT commit
-- ============================================================
USE SalesDB;
BEGIN TRANSACTION;
UPDATE TOP(5000) Orders SET Status = 'Processing' WHERE Status = 'Pending';
-- Leave this transaction open to hold the locks.
-- After the alert email arrives, release the lock with:
--   ROLLBACK TRANSACTION;


-- ============================================================
-- Session 2 (Blocked): run this in a second SSMS window.
-- It waits on Session 1 until Session 1 commits or rolls back.
-- ============================================================
-- USE SalesDB;
-- SELECT CustomerID, SUM(TotalAmount) AS Total
-- FROM Orders WHERE Status = 'Pending'
-- GROUP BY CustomerID;
