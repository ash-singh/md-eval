# Caching proposal

We should add caching to the product service because it's slow.

We'll use Redis. The service will check Redis first and if the data isn't there it will go to
the database and then put it in Redis. This is a common pattern and it works well.

We should do this soon. Let me know if there are questions.
