"""Explicit bounded worker for the candidate inbox. Does not run on web import."""
import json,time
from django.core.management.base import BaseCommand,CommandError
from api.natural_memory_background import worker_once
from api.natural_memory_contract import NaturalMemoryContractError

class Command(BaseCommand):
    help='处理已排队的自然语言记忆任务；不会扫描历史聊天或自动重试。'
    def add_arguments(self,parser):
        parser.add_argument('--max-tasks',type=int,default=1)
        parser.add_argument('--loop',action='store_true')
    def handle(self,*args,**options):
        while True:
            try:result=worker_once(max_tasks=options['max_tasks'])
            except NaturalMemoryContractError as error:raise CommandError(str(error))
            if result or not options['loop']:self.stdout.write(json.dumps({'results':result},ensure_ascii=False))
            if not options['loop']:return
            time.sleep(1)
