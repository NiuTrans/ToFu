CREATE FUNCTION public.tofu_worker_lease_clock_v1()
RETURNS jsonb
AS 'MODULE_PATHNAME', 'tofu_worker_lease_clock_v1'
LANGUAGE C VOLATILE PARALLEL RESTRICTED;
