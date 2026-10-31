"""Process at most three existing tasks. Never schedules or scans all accounts."""
from django.core.management.base import BaseCommand,CommandError
from django.contrib.auth.models import User
from api.models import MemoryMaintenanceTask
from api.memory_lifecycle import run_task

class Command(BaseCommand):
    help='Run bounded existing memory maintenance tasks for one explicit account.'
    def add_arguments(self,parser):parser.add_argument('--user-id',type=int,required=True)
    def handle(self,*args,**options):
        user=User.objects.filter(pk=options['user_id']).first()
        if not user:raise CommandError('Account not found')
        for task in MemoryMaintenanceTask.objects.filter(user=user,status__in=['queued','running','failed']).order_by('id')[:3]:
            result=run_task(user,task.id);self.stdout.write(f'{result.id}: {result.status}')
