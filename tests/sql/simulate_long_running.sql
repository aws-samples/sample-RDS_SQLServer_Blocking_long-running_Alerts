-- simulate_long_running.sql
-- Runs a resource-intensive cross-join that executes well beyond the
-- LONG_RUNNING_THRESHOLD_SECONDS so the next Lambda invocation detects it.
-- Run against a NON-production user database.

USE SalesDB;
SELECT c.Country, c.City, o.Status, o.PaymentMethod,
       COUNT(DISTINCT c.CustomerID) AS UniqueCustomers,
       SUM(oi.Quantity * oi.UnitPrice) AS Revenue
FROM Customers c
JOIN Orders o ON c.CustomerID = o.CustomerID
JOIN OrderItems oi ON o.OrderID = oi.OrderID
CROSS JOIN Orders o2
WHERE o2.OrderID % 1000 = 0
GROUP BY c.Country, c.City, o.Status, o.PaymentMethod
ORDER BY Revenue DESC;
