from django.contrib import admin
from .models import (
    ActivityLog,
    Attachment,
    Category,
    Complaint,
    Followup,
    Notification,
    Priority,
    Project,
    ProjectActivity,
    Reaction,
    Section,
    Sprint,
    Status,
    TeamMember,
)

admin.site.register(TeamMember)
admin.site.register(Complaint)
admin.site.register(Followup)
admin.site.register(ActivityLog)
admin.site.register(Notification)
admin.site.register(Status)
admin.site.register(Priority)
admin.site.register(Category)
admin.site.register(Project)
admin.site.register(ProjectActivity)
admin.site.register(Attachment)
admin.site.register(Reaction)
admin.site.register(Sprint)
admin.site.register(Section)