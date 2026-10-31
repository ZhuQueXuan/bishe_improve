from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'

    def ready(self):
        from django.conf import settings
        if getattr(settings,'NATURAL_MEMORY_FORMAL_ENABLED',False) and getattr(settings,'NATURAL_MEMORY_RELEASE_READY',False):
            from .natural_memory_release import install
            install()
