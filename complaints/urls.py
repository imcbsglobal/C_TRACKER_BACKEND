from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AttachmentViewSet,
    CategoryViewSet,
    ComplaintTaskViewSet,
    ComplaintViewSet,
    ConfigView,
    FollowupViewSet,
    LoginView,
    LogoutView,
    MeView,
    NotificationViewSet,
    PriorityViewSet,
    ProjectViewSet,
    SectionViewSet,
    SprintViewSet,
    StatusViewSet,
    TaskFollowupViewSet,
    TeamMemberViewSet,
    UserViewSet,
)

router = DefaultRouter()
router.register("complaints", ComplaintViewSet, basename="complaint")
router.register("complaint-tasks", ComplaintTaskViewSet, basename="complaint-task")
router.register("task-followups", TaskFollowupViewSet, basename="task-followup")
router.register("team", TeamMemberViewSet, basename="team")
router.register("followups", FollowupViewSet, basename="followup")
router.register("attachments", AttachmentViewSet, basename="attachment")
router.register("users", UserViewSet, basename="user")
router.register("notifications", NotificationViewSet, basename="notification")
router.register("statuses", StatusViewSet, basename="status")
router.register("priorities", PriorityViewSet, basename="priority")
router.register("categories", CategoryViewSet, basename="category")
router.register("projects", ProjectViewSet, basename="project")
router.register("sections", SectionViewSet, basename="section")
router.register("sprints", SprintViewSet, basename="sprint")

urlpatterns = [
    path("auth/login/", LoginView.as_view()),
    path("auth/logout/", LogoutView.as_view()),
    path("auth/me/", MeView.as_view()),
    path("config/", ConfigView.as_view()),
    path("", include(router.urls)),
]