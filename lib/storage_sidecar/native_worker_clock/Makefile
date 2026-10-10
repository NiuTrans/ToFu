EXTENSION = tofu_worker_clock
MODULE_big = tofu_worker_clock
OBJS = worker_clock.o
DATA = tofu_worker_clock--1.0.sql
PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
