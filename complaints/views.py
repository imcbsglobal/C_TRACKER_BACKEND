import uuid

from django.contrib.auth import authenticate, get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    ActivityLog,
    Attachment,
    Category,
    Complaint,
    ComplaintSeen,
    ComplaintTask,
    Followup,
    TaskFollowup,
    Notification,
    Priority,
    Project,
    ProjectActivity,
    REACTION_KEYS,
    Reaction,
    Section,
    Sprint,
    PROTECTED_STATUSES,
    Status,
    TeamMember,
    ensure_core_statuses,
    r2_delete,
    r2_enabled,
    r2_upload,
    seed_defaults,
)
from .serializers import (
    AttachmentSerializer,
    CategorySerializer,
    ComplaintSerializer,
    ComplaintTaskSerializer,
    FollowupSerializer,
    TaskFollowupSerializer,
    NotificationSerializer,
    PrioritySerializer,
    ProjectSerializer,
    SectionSerializer,
    SprintSerializer,
    StatusSerializer,
    TeamMemberSerializer,
    UserSerializer,
    DEV_KINDS,
    allowed_kinds,
    can_edit_complaint,
    default_choice,
    dev_side_members,
    display_name,
    ensure_member,
    user_access,
)

User = get_user_model()

# Images on follow-ups
MAX_IMAGES = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_NOTE = "(image)"  # note used when a follow-up is only pictures


def sniff_image(head):
    """(content_type, extension) from the file's first bytes, or None. The client's
    own file type is never trusted."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None

def check_images(request):
    """Validate every uploaded image before anything is saved.
    Returns [(file, content_type, extension)]."""
    checked = []
    images = request.FILES.getlist("images")
    if images:
        if not r2_enabled():
            raise ValidationError({"images": "Image uploads are not set up on the server."})
        if len(images) > MAX_IMAGES:
            raise ValidationError({"images": f"You can attach up to {MAX_IMAGES} images."})
        for f in images:
            if f.size > MAX_IMAGE_BYTES:
                raise ValidationError({"images": "Each image must be 10 MB or smaller."})
            kind = sniff_image(f.read(16))
            f.seek(0)
            if kind is None:
                raise ValidationError({"images": "Only JPG, PNG, GIF or WebP images are allowed."})
            checked.append((f, kind[0], kind[1]))
    return checked


def closed_status_values():
    """Statuses the admin marked as "done" - they don't count as open tasks."""
    seed_defaults()
    return list(Status.objects.filter(is_closed=True).values_list("value", flat=True))

# Fields whose changes are written to the history log.
TRACKED_FIELDS = {
    "title": "Title",
    "status": "Status",
    "priority": "Priority",
    "category": "Category",
    "project": "Project",
    "section": "Section",
    "sprint": "Sprint",
    "due_date": "Due date",
    "customer": "Customer",
    "phone": "Phone",
}


def show(field, value):
    if value in (None, ""):
        return "empty"
    if field in ("status", "priority"):
        model = Status if field == "status" else Priority
        label = model.objects.filter(value=value).values_list("label", flat=True).first()
        return label or str(value).replace("_", " ").title()
    return str(value)[:80]


def notify_member(complaint, member, actor, text):
    """Direct notice to one team member. Returns their user id if one was sent."""
    if member is None or member.user_id is None or member.user_id == actor.id:
        return None
    Notification.objects.create(
        recipient=member.user,
        complaint=complaint,
        message=text[:300],
        actor_name=display_name(actor),
    )
    return member.user_id


def project_side_members(project, kind):
    """Members of a project who work on this type of item (complaints -> support
    staff, tasks and bugs -> developers) and have an active login."""
    if project is None:
        return []
    side = ("support", "both") if kind == "complaint" else ("developer", "both")
    return list(
        project.members.filter(
            access__in=side, user__isnull=False, user__is_active=True
        ).select_related("user")
    )


def notify_involved(complaint, actor, text, skip=(), extra_users=()):
    """Tell everyone on the task: admin(s), assignee, helpers and creator.

    The person who made the change is never notified. `skip` holds user ids that
    already got a more specific notice; `extra_users` adds people no longer on the
    task (e.g. the previous assignee).
    """
    users = {u.id: u for u in User.objects.filter(is_superuser=True, is_active=True)}
    if complaint.assigned_to and complaint.assigned_to.user_id:
        users[complaint.assigned_to.user_id] = complaint.assigned_to.user
    for m in TeamMember.objects.filter(helping=complaint).select_related("user"):
        if m.user_id:
            users[m.user_id] = m.user
    if complaint.created_by_id:
        users[complaint.created_by_id] = complaint.created_by
    for u in extra_users:
        if u:
            users[u.id] = u
    if actor.is_superuser:  # when the admin acts, the whole project side hears about it
        for m in project_side_members(complaint.project, complaint.kind):
            users[m.user_id] = m.user
    users.pop(actor.id, None)
    for uid in skip:
        users.pop(uid, None)

    name = display_name(actor)
    Notification.objects.bulk_create(
        [
            Notification(
                recipient=u, complaint=complaint, message=text[:300], actor_name=name
            )
            for u in users.values()
        ]
    )


def notify_assignment(complaint, actor):
    """Direct notice to the newly assigned person. Returns their user id if sent."""
    return notify_member(
        complaint,
        complaint.assigned_to,
        actor,
        f'{display_name(actor)} assigned you "{complaint.title}"',
    )


def sync_helper_log(complaint, actor, before_ids):
    """Log helper changes and notify added helpers directly.

    Returns (log entries, user ids notified directly, users of removed helpers).
    """
    after = {m.id: m for m in TeamMember.objects.filter(helping=complaint)}  # fresh query, not the prefetch cache
    name = display_name(actor)
    entries, notified, removed_users = [], set(), []
    for mid, member in after.items():
        if mid not in before_ids:
            entries.append(f"{member.full_name} added as helper by {name}")
            uid = notify_member(
                complaint,
                member,
                actor,
                f'{name} added you as a helper on "{complaint.title}"',
            )
            if uid:
                notified.add(uid)
    for member in TeamMember.objects.filter(
        id__in=set(before_ids) - set(after)
    ).select_related("user"):
        entries.append(f"{member.full_name} removed as helper by {name}")
        if member.user_id:
            removed_users.append(member.user)
    ActivityLog.objects.bulk_create(
        [ActivityLog(complaint=complaint, message=m[:300], actor_name=name) for m in entries]
    )
    return entries, notified, removed_users


def notify_followup(complaint, actor, note):
    notify_involved(
        complaint,
        actor,
        f'{display_name(actor)} added a follow-up on "{complaint.title}": {note}',
    )


def notify_task_pickup(c, actor):
    """A developer took an unassigned task / bug. Tell the admin(s) and the whole
    developer team of the project. When the task was escalated from support, also tell
    the support side (the case's assignee, helpers, creator and the support project's
    team) on the original case, and leave a line in that case's history."""
    name = display_name(actor)
    users = {u.id: u for u in User.objects.filter(is_superuser=True, is_active=True)}
    for m in project_side_members(c.project, c.kind):
        users[m.user_id] = m.user
    users.pop(actor.id, None)
    notify_users(
        users.values(), f'{name} self-assigned "{c.title}"', complaint=c, actor_name=name
    )

    links = c.escalated_from_tasks.select_related(
        "complaint__assigned_to__user", "complaint__created_by", "complaint__project"
    )
    for t in links:
        case = t.complaint
        support = {}
        if case.assigned_to and case.assigned_to.user_id:
            support[case.assigned_to.user_id] = case.assigned_to.user
        for m in TeamMember.objects.filter(helping=case).select_related("user"):
            if m.user_id:
                support[m.user_id] = m.user
        if case.created_by_id:
            support[case.created_by_id] = case.created_by
        for m in project_side_members(case.project, "complaint"):
            support[m.user_id] = m.user
        for uid in list(support):
            if uid == actor.id or uid in users:  # the admin and developers already heard
                support.pop(uid)
        ActivityLog.objects.create(
            complaint=case,
            message=f'Escalated task "{t.name}": {name} (developers) picked it up'[:300],
            actor_name=name,
        )
        notify_users(
            support.values(),
            f'{name} (developers) picked up the escalated task "{t.name}" on "{case.title}"',
            complaint=case,
            actor_name=name,
        )


def notify_project_members(project, members, actor, text, link=True):
    """One on-screen notice per team member who has a login (never the actor).

    link=False leaves the project off the notice, for people who can no longer
    open it (removed from the project, or the project was deleted).
    """
    name = display_name(actor)
    Notification.objects.bulk_create(
        [
            Notification(
                recipient_id=m.user_id,
                project=project if link else None,
                message=text[:300],
                actor_name=name,
            )
            for m in members
            if m.user_id and m.user_id != actor.id
        ]
    )


def log_project(project, message, actor, kind="change", task_kind=""):
    """Write one line to a project's history. `actor` is a user or a plain name.
    `task_kind` marks lines about a complaint/task/bug so each side only sees its own."""
    if project is None:
        return
    ProjectActivity.objects.create(
        project=project,
        kind=kind,
        task_kind=task_kind,
        message=message[:300],
        actor_name=actor if isinstance(actor, str) else display_name(actor),
    )


def task_ref(complaint):
    """How a task is named inside a project's history, e.g. #TASK12 "Fix login"."""
    return f'#TASK{complaint.id} "{complaint.title[:60]}"'


class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


# ---------- auth ----------

class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip()
        password = request.data.get("password") or ""

        found = User.objects.filter(
            Q(email__iexact=email) | Q(username__iexact=email)
        ).first()
        user = (
            authenticate(request, username=found.username, password=password)
            if found
            else None
        )
        if user is None:
            return Response(
                {"detail": "Invalid email or password."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ensure_member(user)  # links logins created before assignment support
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "user": UserSerializer(user).data})


class LogoutView(APIView):
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class UserViewSet(viewsets.ModelViewSet):
    """Admin only: create and manage user logins."""

    serializer_class = UserSerializer
    permission_classes = [IsSuperAdmin]

    def get_queryset(self):
        return User.objects.filter(is_superuser=False).order_by("first_name", "email")


# ---------- team ----------

class TeamMemberViewSet(viewsets.ModelViewSet):
    serializer_class = TeamMemberSerializer

    def get_permissions(self):
        # Everyone logged in can read the team (assignee dropdowns).
        # Only the admin can add, edit or remove members.
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsSuperAdmin()]

    def get_queryset(self):
        user = self.request.user
        qs = TeamMember.objects.annotate(
            open_tasks=Count(
                "complaints",
                filter=Q(complaints__kind__in=allowed_kinds(user))
                & ~Q(complaints__status__in=closed_status_values()),
            )
        )
        # Each side only sees its own people in dropdowns.
        access = user_access(user)
        if not user.is_superuser and access == "support":
            qs = qs.filter(access__in=["support", "both"])
        elif not user.is_superuser and access == "developer":
            qs = qs.filter(access__in=["developer", "both"])
        return qs


# ---------- complaints ----------

def visible_to(user, qs, prefix=""):
    """Admin sees everything. Everyone else needs to be a member of the project, and
    then sees only their side of it:

    Support    -> its complaints
    Developer  -> its tasks and bugs (never complaints)
    Both       -> everything in the project
    """
    if user.is_superuser:
        return qs
    in_project = Q(**{f"{prefix}project__members__user": user})
    access = user_access(user)
    if access == "support":
        q = in_project & Q(**{f"{prefix}kind": "complaint"})
    elif access == "both":
        q = in_project
    else:
        q = in_project & Q(**{f"{prefix}kind__in": DEV_KINDS})
    return qs.filter(q).distinct()


class ComplaintViewSet(viewsets.ModelViewSet):
    serializer_class = ComplaintSerializer
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        seed_defaults()
        qs = Complaint.objects.select_related("assigned_to", "escalated_to", "project", "section", "sprint", "created_by").prefetch_related(
            "followups__attachments", "followups__reactions__user", "attachments", "helpers", "tasks__followups", "tasks__dev_task__assigned_to", "escalated_from_tasks__complaint"
        )
        qs = visible_to(self.request.user, qs)

        params = self.request.query_params
        if params.get("kind"):
            qs = qs.filter(kind=params["kind"])
        if params.get("project"):
            qs = qs.filter(project_id=params["project"])
        if params.get("section"):
            if params["section"] == "none":  # tasks not under any section
                qs = qs.filter(section__isnull=True)
            elif params["section"].isdigit():
                qs = qs.filter(section_id=params["section"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("priority"):
            qs = qs.filter(priority=params["priority"])
        if params.get("assigned_to"):
            qs = qs.filter(assigned_to_id=params["assigned_to"])
        if params.get("sprint"):
            if params["sprint"] == "none":  # the backlog
                qs = qs.filter(sprint__isnull=True)
            elif params["sprint"].isdigit():
                qs = qs.filter(sprint_id=params["sprint"])
        if params.get("search"):
            term = params["search"]
            qs = qs.filter(
                Q(title__icontains=term)
                | Q(description__icontains=term)
                | Q(customer__icontains=term)
            )
        return qs

    def retrieve(self, request, *args, **kwargs):
        """Opening a task puts out its red light for this user."""
        instance = self.get_object()
        if not request.user.is_superuser:
            ComplaintSeen.objects.get_or_create(complaint=instance, user=request.user)
        return Response(self.get_serializer(instance).data)

    def create(self, request, *args, **kwargs):
        """File a case. Send JSON, or multipart (the fields + up to 5 "images") so the
        description can come with pictures the assignee sees when they open the case."""
        checked = check_images(request)  # a bad file never leaves a half-saved case
        self._image_count = len(checked)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        stored = []
        try:
            with transaction.atomic():
                self.perform_create(serializer)
                obj = serializer.instance
                for f, content_type, ext in checked:
                    key = f"complaints/{obj.id}/{uuid.uuid4().hex}.{ext}"
                    url = r2_upload(f, key, content_type)
                    stored.append(key)
                    Attachment.objects.create(
                        complaint=obj,
                        key=key,
                        url=url,
                        name=(f.name or "image")[:255],
                        content_type=content_type,
                        size=f.size,
                        uploaded_by=request.user,
                    )
        except Exception:
            for key in stored:  # roll back anything already in the bucket
                try:
                    r2_delete(key)
                except Exception:
                    pass
            raise ValidationError({"images": "Could not store the images. Please try again."})

        fresh = self.get_queryset().get(pk=serializer.instance.pk)
        return Response(self.get_serializer(fresh).data, status=status.HTTP_201_CREATED)

    def perform_create(self, serializer):
        user = self.request.user
        obj = serializer.save(created_by=user, created_by_name=display_name(user))
        actor = display_name(user)

        changes, skip = [], set()
        if obj.assigned_to:
            msg = f"Assigned to {obj.assigned_to.full_name} by {actor}"
            ActivityLog.objects.create(complaint=obj, message=msg[:300], actor_name=actor)
            changes.append(msg)
            uid = notify_assignment(obj, user)
            if uid:
                skip.add(uid)

        helper_entries, helper_skip, _ = sync_helper_log(obj, user, set())
        changes += helper_entries
        skip |= helper_skip

        kind_word = {"complaint": "complaint", "bug": "bug", "task": "task"}.get(obj.kind, "issue")
        text = f'{actor} filed a {kind_word}: "{obj.title}"'
        n_img = getattr(self, "_image_count", 0)
        if n_img:
            text += f" (with {n_img} image{'s' if n_img != 1 else ''})"
        if changes:
            text += ": " + "; ".join(changes)
        notify_involved(obj, user, text, skip=skip)

        # A task filed in an owned section: its owners hear about it too.
        if obj.section_id and obj.kind in DEV_KINDS:
            owners = [
                m.user
                for m in obj.section.owners.select_related("user")
                if m.user_id and m.user.is_active and m.user_id != user.id and m.user_id not in skip
            ]
            notify_users(
                owners,
                f'{actor} added "{obj.title}" to your section "{obj.section.name}"',
                complaint=obj,
                actor_name=actor,
            )

    def perform_update(self, serializer):
        old = serializer.instance
        # Support cases: everyone on the team can see them, only owners can edit.
        if not can_edit_complaint(self.request.user, old):
            raise PermissionDenied(
                "Self-assign this case to edit it."
                if old.assigned_to_id is None
                else "Only the assignee, helpers or the person who filed this case can edit it."
            )
        before = {f: getattr(old, f) for f in TRACKED_FIELDS}
        before_description = old.description
        before_assignee = old.assigned_to.full_name if old.assigned_to else None
        before_assignee_user = (
            old.assigned_to.user if old.assigned_to and old.assigned_to.user_id else None
        )
        before_helpers = set(old.helpers.values_list("id", flat=True))

        obj = serializer.save()

        # Moving a task between projects is written to both projects' history.
        if before["project"] != obj.project:
            ref = task_ref(obj)
            old_p, new_p = before["project"], obj.project
            log_project(
                old_p,
                f'Task {ref} moved to {new_p.name if new_p else "no project"}',
                self.request.user,
                task_kind=obj.kind,
            )
            log_project(
                new_p,
                f'Task {ref} moved in from {old_p.name if old_p else "no project"}',
                self.request.user,
                task_kind=obj.kind,
            )

        messages = []
        for field, label in TRACKED_FIELDS.items():
            new = getattr(obj, field)
            if new != before[field]:
                messages.append(
                    f"{label} changed from {show(field, before[field])} to {show(field, new)}"
                )
        if obj.description != before_description:
            messages.append("Description updated")

        # Support cases: when a case reaches a "done" status, leave a line in the log
        # (and in the in-app notice) so the customer gets informed.
        if obj.kind == "complaint" and obj.status != before["status"]:
            done = Status.objects.filter(value=obj.status, is_closed=True).first()
            was_done = Status.objects.filter(
                value=before["status"], is_closed=True
            ).exists()
            if done and not was_done:
                who = obj.customer or "the customer"
                messages.append(f"Case marked {done.label} - inform {who}")

        actor = display_name(self.request.user)

        # Sprint work: when a task reaches a "done" status, tell the admin if it was
        # finished in time or late. They get this notice instead of the generic one below.
        done_in_sprint = (
            obj.kind in DEV_KINDS
            and obj.sprint_id is not None
            and obj.status != before["status"]
            and Status.objects.filter(value=obj.status, is_closed=True).exists()
            and not Status.objects.filter(value=before["status"], is_closed=True).exists()
        )
        if done_in_sprint:
            messages.append(f"Completed in sprint ({task_timing(obj)})")

        after_assignee = obj.assigned_to.full_name if obj.assigned_to else None
        assignee_changed = after_assignee != before_assignee
        if assignee_changed:
            messages.append(
                f"Assigned to {after_assignee} by {actor}"
                if after_assignee
                else f"Unassigned (was {before_assignee}) by {actor}"
            )

        ActivityLog.objects.bulk_create(
            [ActivityLog(complaint=obj, message=m[:300], actor_name=actor) for m in messages]
        )

        skip = set()
        if done_in_sprint:
            skip |= notify_task_done(obj, self.request.user)
        if assignee_changed and obj.assigned_to:
            uid = notify_assignment(obj, self.request.user)
            if uid:
                skip.add(uid)

        if obj.kind in DEV_KINDS and obj.status != before["status"]:
            notify_escalation_progress(obj, self.request.user)

        helper_entries, helper_skip, removed_users = sync_helper_log(
            obj, self.request.user, before_helpers
        )
        skip |= helper_skip

        changes = messages + helper_entries
        if changes:
            extra = removed_users + ([before_assignee_user] if assignee_changed else [])
            notify_involved(
                obj,
                self.request.user,
                f'{actor} updated "{obj.title}": ' + "; ".join(changes),
                skip=skip,
                extra_users=extra,
            )

    def perform_destroy(self, instance):
        user = self.request.user
        if not user.is_superuser and instance.created_by_id != user.id:
            raise PermissionDenied("Only the person who created this task can delete it.")
        project, ref, kind = instance.project, task_ref(instance), instance.kind
        title = instance.title
        members = project_side_members(project, kind) if user.is_superuser else []
        instance.delete()
        log_project(project, f"Task {ref} deleted", user, task_kind=kind)
        notify_project_members(
            project, members, user, f'{display_name(user)} deleted "{title}"'
        )

    @action(detail=True, methods=["post"], url_path="self-assign")
    def self_assign(self, request, pk=None):
        """A support person picks up an unassigned case, or a developer picks up an
        unassigned task / bug (for example one escalated from support)."""
        c = self.get_object()  # 404 unless they are in the case's project
        dev_item = c.kind in DEV_KINDS
        member = TeamMember.objects.filter(user=request.user).first()
        allowed = ("developer", "both") if dev_item else ("support", "both")
        if member is None or member.access not in allowed:
            raise PermissionDenied(
                "Only developers can self-assign tasks and bugs."
                if dev_item
                else "Only support team members can self-assign cases."
            )
        if c.assigned_to_id:
            raise ValidationError({"detail": "This item is already assigned."})

        c.assigned_to = member
        c.save(update_fields=["assigned_to", "updated_at"])
        actor = display_name(request.user)
        ActivityLog.objects.create(
            complaint=c, message=f"Self-assigned by {actor}"[:300], actor_name=actor
        )
        if dev_item:
            log_project(
                c.project, f"Task {task_ref(c)} self-assigned by {actor}", request.user, task_kind=c.kind
            )
            notify_task_pickup(c, request.user)
        else:
            notify_involved(c, request.user, f'{actor} self-assigned "{c.title}"')
        return Response(self.get_serializer(c).data)

    @action(detail=True, methods=["post"])
    def escalate(self, request, pk=None):
        """Hand a support case to another support person: POST {"member": <team id>}.
        They are added as a helper (so they can work on it) and notified."""
        c = self.get_object()
        if c.kind != "complaint":
            raise ValidationError({"detail": "Only support cases can be escalated."})
        if not can_edit_complaint(request.user, c):
            raise PermissionDenied(
                "Self-assign this case to escalate it."
                if c.assigned_to_id is None
                else "Only the assignee, helpers or the person who filed this case can escalate it."
            )
        if Status.objects.filter(value=c.status, is_closed=True).exists():
            raise ValidationError({"detail": "A closed case can't be escalated. Reopen it first."})

        try:
            member = c.project.members.get(id=int(request.data.get("member")))
        except (TypeError, ValueError, TeamMember.DoesNotExist):
            raise ValidationError({"detail": "Choose a team member from this project."})
        if member.access not in ("support", "both"):
            raise ValidationError({"detail": "Cases can only be escalated to support staff."})
        if member.id == c.assigned_to_id:
            raise ValidationError({"detail": f"{member.full_name} is already assigned to this case."})
        if member.user_id == request.user.id:
            raise ValidationError({"detail": "You can't escalate a case to yourself."})
        if member.id == c.escalated_to_id:
            raise ValidationError({"detail": f"Already escalated to {member.full_name}."})

        c.escalated_to = member
        c.save(update_fields=["escalated_to", "updated_at"])
        c.helpers.add(member)  # so they can see it under "My cases" and edit it

        actor = display_name(request.user)
        ActivityLog.objects.create(
            complaint=c,
            message=f"Escalated to {member.full_name} by {actor}"[:300],
            actor_name=actor,
        )
        skip = set()
        uid = notify_member(c, member, request.user, f'{actor} escalated "{c.title}" to you')
        if uid:
            skip.add(uid)
        notify_involved(
            c, request.user, f'{actor} escalated "{c.title}" to {member.full_name}', skip=skip
        )
        # fresh query: the helpers list on `c` was prefetched before the change
        return Response(self.get_serializer(self.get_queryset().get(pk=c.pk)).data)

    @action(detail=True, methods=["post"], url_path="clear-escalation")
    def clear_escalation(self, request, pk=None):
        """Take the escalation off a case. The person stays on it as a helper."""
        c = self.get_object()
        if c.kind != "complaint":
            raise ValidationError({"detail": "Only support cases can be escalated."})
        if not can_edit_complaint(request.user, c):
            raise PermissionDenied("Only the assignee, helpers or the person who filed this case can do that.")
        if not c.escalated_to_id:
            raise ValidationError({"detail": "This case is not escalated."})

        name = c.escalated_to.full_name
        c.escalated_to = None
        c.save(update_fields=["escalated_to", "updated_at"])
        actor = display_name(request.user)
        ActivityLog.objects.create(
            complaint=c,
            message=f"Escalation to {name} cleared by {actor}"[:300],
            actor_name=actor,
        )
        notify_involved(c, request.user, f'{actor} cleared the escalation on "{c.title}"')
        return Response(self.get_serializer(self.get_queryset().get(pk=c.pk)).data)

    @action(
        detail=True,
        methods=["post"],
        parser_classes=[JSONParser, MultiPartParser, FormParser],
    )
    def followups(self, request, pk=None):
        """Add a follow-up. Send JSON ({"note"}) or multipart (note + up to 5 "images")."""
        complaint = self.get_object()  # 404 if it isn't theirs

        # Check every image first, so a bad file never leaves a half-saved follow-up.
        checked = []
        images = request.FILES.getlist("images")
        if images:
            if not r2_enabled():
                raise ValidationError({"images": "Image uploads are not set up on the server."})
            if len(images) > MAX_IMAGES:
                raise ValidationError({"images": f"You can attach up to {MAX_IMAGES} images."})
            for f in images:
                if f.size > MAX_IMAGE_BYTES:
                    raise ValidationError({"images": "Each image must be 10 MB or smaller."})
                kind = sniff_image(f.read(16))
                f.seek(0)
                if kind is None:
                    raise ValidationError({"images": "Only JPG, PNG, GIF or WebP images are allowed."})
                checked.append((f, kind[0], kind[1]))

        data = request.data.copy()
        if checked and not str(data.get("note") or "").strip():
            data["note"] = IMAGE_NOTE

        serializer = FollowupSerializer(data=data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        stored = []
        try:
            with transaction.atomic():
                followup = serializer.save(
                    complaint=complaint, created_by_name=display_name(request.user)
                )
                for f, content_type, ext in checked:
                    key = f"complaints/{complaint.id}/{uuid.uuid4().hex}.{ext}"
                    url = r2_upload(f, key, content_type)
                    stored.append(key)
                    Attachment.objects.create(
                        followup=followup,
                        key=key,
                        url=url,
                        name=(f.name or "image")[:255],
                        content_type=content_type,
                        size=f.size,
                        uploaded_by=request.user,
                    )
        except Exception:
            for key in stored:  # roll back anything already in the bucket
                try:
                    r2_delete(key)
                except Exception:
                    pass
            raise ValidationError({"images": "Could not store the images. Please try again."})

        notify_followup(complaint, request.user, serializer.validated_data["note"])
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        """Newest first: field changes, follow-ups, and the creation event."""
        c = self.get_object()  # same visibility rules as everything else

        entries = [
            {
                "id": f"log-{a.id}",
                "type": "change",
                "actor": a.actor_name,
                "message": a.message,
                "created_at": a.created_at,
            }
            for a in c.activity.all()
        ]
        entries += [
            {
                "id": f"followup-{f.id}",
                "type": "followup",
                "actor": f.created_by_name,
                "message": f.note,
                "created_at": f.created_at,
            }
            for f in c.followups.all()
        ]
        entries.append(
            {
                "id": "created",
                "type": "created",
                "actor": c.created_by_name,
                "message": "Task created",
                "created_at": c.created_at,
            }
        )
        entries.sort(key=lambda e: e["created_at"], reverse=True)
        return Response(entries)


class ComplaintTaskViewSet(viewsets.ModelViewSet):
    """Tasks (name + goal) that a support person adds to a complaint.

    GET    /complaint-tasks/?complaint=<id>&project=<id>
    POST   /complaint-tasks/        body {"complaint": id, "name", "goal", "status"?}
    PATCH  /complaint-tasks/<id>/   body {"name"?, "goal"?, "status"?}
    DELETE /complaint-tasks/<id>/

    Everyone who can see the complaint sees its tasks. Only the people who can edit
    the complaint (assignee, helpers, the person who filed it, the admin) change them.
    """

    serializer_class = ComplaintTaskSerializer
    pagination_class = None

    def get_queryset(self):
        seed_defaults()
        qs = ComplaintTask.objects.select_related(
            "complaint", "complaint__assigned_to", "complaint__project", "dev_task", "dev_task__assigned_to"
        ).prefetch_related("complaint__helpers", "followups")
        qs = visible_to(self.request.user, qs, prefix="complaint__").filter(
            complaint__kind="complaint"
        )
        params = self.request.query_params
        if params.get("complaint", "").isdigit():
            qs = qs.filter(complaint_id=params["complaint"])
        if params.get("project", "").isdigit():
            qs = qs.filter(complaint__project_id=params["project"])
        return qs

    def _touch(self, complaint):
        Complaint.objects.filter(pk=complaint.pk).update(updated_at=timezone.now())

    def _log(self, complaint, message, actor_user, notify=True):
        actor = display_name(actor_user)
        ActivityLog.objects.create(complaint=complaint, message=message[:300], actor_name=actor)
        if notify:
            notify_involved(complaint, actor_user, f'{actor} - "{complaint.title}": {message}')

    def perform_create(self, serializer):
        user = self.request.user
        obj = serializer.save(created_by=user, created_by_name=display_name(user))
        self._touch(obj.complaint)
        self._log(obj.complaint, f'Task added: "{obj.name}"', user)

    def perform_update(self, serializer):
        user = self.request.user
        old = serializer.instance
        if not can_edit_complaint(user, old.complaint):
            raise PermissionDenied("Only the assignee, helpers or the person who filed this complaint can edit its tasks.")
        before = {"name": old.name, "goal": old.goal, "status": old.status}
        obj = serializer.save()
        self._touch(obj.complaint)

        if obj.status != before["status"]:
            self._log(
                obj.complaint,
                f'Task "{obj.name}": status changed from {show("status", before["status"])} to {show("status", obj.status)}',
                user,
            )
        if obj.name != before["name"] or obj.goal != before["goal"]:
            self._log(obj.complaint, f'Task "{before["name"]}" updated', user, notify=False)

    @action(detail=True, methods=["post"])
    def escalate(self, request, pk=None):
        """Hand one task of a support case up the chain.

        POST {"target": "admin" | "developers", "note"?, "kind"?: "bug" | "task", "priority"?}

        admin       -> the admin(s) are notified.
        developers  -> a task / bug is created in the development project that has the
                       SAME NAME as this case's support project, and every developer on
                       that project is notified. Refused when there is no such project.
        """
        t = self.get_object()  # 404 unless the case is theirs
        c = t.complaint
        user = request.user
        if not can_edit_complaint(user, c):
            raise PermissionDenied(
                "Only the assignee, helpers or the person who filed this complaint can escalate its tasks."
            )
        closed = set(closed_status_values())
        if t.status in closed:
            raise ValidationError({"detail": "A finished task can't be escalated. Reopen it first."})
        if c.status in closed:
            raise ValidationError({"detail": "This complaint is closed. Reopen it to escalate its tasks."})

        target = request.data.get("target")
        note = str(request.data.get("note") or "").strip()[:500]
        actor = display_name(user)

        if target == "admin":
            if user.is_superuser:
                raise ValidationError({"detail": "You are the admin."})
            if t.escalation == "admin":
                raise ValidationError({"detail": "This task is already escalated to the admin."})
            t.escalation, t.dev_task = "admin", None
            t.escalated_at, t.escalated_by_name, t.escalation_note = timezone.now(), actor, note
            t.save(update_fields=["escalation", "dev_task", "escalated_at", "escalated_by_name", "escalation_note", "updated_at"])
            ActivityLog.objects.create(
                complaint=c, message=f'Task "{t.name}" escalated to the admin by {actor}'[:300], actor_name=actor
            )
            text = f'{actor} escalated the task "{t.name}" on "{c.title}" to the admin'
            notify_involved(c, user, text + (f": {note}" if note else ""))

        elif target == "developers":
            if t.escalation == "developers" and t.dev_task_id and t.dev_task.status not in closed:
                raise ValidationError({"detail": "This task is already with the developer team."})
            twin = c.project.twin() if c.project_id else None
            if twin is None:
                raise ValidationError(
                    {
                        "detail": f'"{c.project.name if c.project_id else "This project"}" has no development side. '
                        "Ask the admin to create a development project with the same name."
                    }
                )
            devs = dev_side_members(twin)
            if not devs:
                raise ValidationError(
                    {"detail": f'No developers are on the development project "{twin.name}" yet. Ask the admin to add them.'}
                )
            kind = request.data.get("kind")
            if kind not in DEV_KINDS:
                kind = "bug"
            priority = request.data.get("priority")
            if not Priority.objects.filter(value=priority).exists():
                priority = c.priority

            body = [f'Escalated from support case {c.case_number or c.id}: "{c.title}"', f"Support task: {t.name}"]
            if t.goal:
                body.append(f"Goal: {t.goal}")
            if c.description:
                body.append(f"\nIssue reported:\n{c.description}")
            if note:
                body.append(f"\nNote from {actor}:\n{note}")
            # Customer name and phone are never copied: they don't reach developers.

            # Developers and the admin open a development project through its sprints, so a
            # task with no sprint would be invisible to them. Put it in the project's active
            # sprint when there is one (otherwise it waits in the backlog).
            active_sprint = (
                Sprint.objects.filter(projects=twin, status="active").order_by("-start_date", "-id").first()
            )

            with transaction.atomic():
                dev = Complaint.objects.create(
                    kind=kind,
                    title=f"{c.case_number or c.id}: {t.name}"[:200],
                    description="\n".join(body),
                    priority=priority,
                    status=default_choice(Status, "open"),
                    project=twin,
                    sprint=active_sprint,
                    created_by_name=f"{actor} (Support)",
                )
                t.escalation, t.dev_task = "developers", dev
                t.escalated_at, t.escalated_by_name, t.escalation_note = timezone.now(), actor, note
                t.save(update_fields=["escalation", "dev_task", "escalated_at", "escalated_by_name", "escalation_note", "updated_at"])
                ActivityLog.objects.create(
                    complaint=c,
                    message=f'Task "{t.name}" escalated to the {twin.name} developer team by {actor}'[:300],
                    actor_name=actor,
                )
                ActivityLog.objects.create(
                    complaint=dev,
                    message=f"Escalated from support case {c.case_number or c.id} by {actor}"[:300],
                    actor_name=actor,
                )
                if active_sprint:
                    ActivityLog.objects.create(
                        complaint=dev,
                        message=f'Added to sprint "{active_sprint.name}" automatically'[:300],
                        actor_name=actor,
                    )
                log_project(twin, f"Task {task_ref(dev)} escalated from support case {c.case_number or c.id}", user, task_kind=kind)
                log_project(c.project, f'Task "{t.name}" on {c.case_number or c.id} escalated to developers', user, task_kind="complaint")

            # Developers hear about it on the new task. The support side and the admin
            # hear about it on the case (admins are included by notify_involved).
            notify_users(
                [m.user for m in devs if m.user_id != user.id],
                f'{actor} escalated "{t.name}" from support case {c.case_number or c.id} to the developers: "{dev.title}"',
                complaint=dev,
                actor_name=actor,
            )
            notify_involved(
                c, user, f'{actor} escalated the task "{t.name}" on "{c.title}" to the {twin.name} developer team'
            )
        else:
            raise ValidationError({"detail": "Choose Admin or the developer team."})

        self._touch(c)
        t.refresh_from_db()
        return Response(self.get_serializer(t).data)

    def perform_destroy(self, instance):
        user = self.request.user
        if not can_edit_complaint(user, instance.complaint):
            raise PermissionDenied("Only the assignee, helpers or the person who filed this complaint can delete its tasks.")
        complaint, name = instance.complaint, instance.name
        instance.delete()
        self._touch(complaint)
        self._log(complaint, f'Task removed: "{name}"', user)


def notify_escalation_progress(dev_task, actor):
    """A developer changed the status of a task that support escalated. Tell the
    support people on the original case (assignee, helpers, whoever filed it) and
    leave a line in that case's history, so support can update the customer."""
    links = list(
        dev_task.escalated_from_tasks.select_related(
            "complaint__assigned_to__user", "complaint__created_by"
        )
    )
    if not links:
        return
    name = display_name(actor)
    label = show("status", dev_task.status)
    for t in links:
        c = t.complaint
        text = f'{name} moved the escalated task "{t.name}" ({c.title}) to {label}'
        ActivityLog.objects.create(
            complaint=c,
            message=f'Escalated task "{t.name}": developers set it to {label} ({name})'[:300],
            actor_name=name,
        )
        users = {}
        if c.assigned_to and c.assigned_to.user_id:
            users[c.assigned_to.user_id] = c.assigned_to.user
        for m in TeamMember.objects.filter(helping=c).select_related("user"):
            if m.user_id:
                users[m.user_id] = m.user
        if c.created_by_id:
            users[c.created_by_id] = c.created_by
        users.pop(actor.id, None)
        notify_users(users.values(), text, complaint=c, actor_name=name)


class TaskFollowupViewSet(viewsets.ModelViewSet):
    """Follow-ups on one task of a support complaint.

    GET    /task-followups/?task=<id>
    POST   /task-followups/        body {"task": id, "note"}
    PATCH  /task-followups/<id>/   body {"note"}      (author or admin)
    DELETE /task-followups/<id>/                        (author or admin)

    Anyone who can see the complaint can write a follow-up on its tasks.
    """

    serializer_class = TaskFollowupSerializer
    pagination_class = None

    def get_queryset(self):
        seed_defaults()
        qs = TaskFollowup.objects.select_related("task", "task__complaint")
        qs = visible_to(self.request.user, qs, prefix="task__complaint__").filter(
            task__complaint__kind="complaint"
        )
        if self.request.query_params.get("task", "").isdigit():
            qs = qs.filter(task_id=self.request.query_params["task"])
        return qs

    def _own(self, instance):
        user = self.request.user
        if not (user.is_superuser or instance.created_by_id == user.id):
            raise PermissionDenied("You can only change your own follow-ups.")

    def perform_create(self, serializer):
        user = self.request.user
        task = serializer.validated_data["task"]
        allowed = visible_to(
            user,
            ComplaintTask.objects.filter(pk=task.pk, complaint__kind="complaint"),
            prefix="complaint__",
        )
        if not allowed.exists():
            raise PermissionDenied("You can't add follow-ups to this task.")
        obj = serializer.save(created_by=user, created_by_name=display_name(user))
        complaint = task.complaint
        Complaint.objects.filter(pk=complaint.pk).update(updated_at=timezone.now())
        actor = display_name(user)
        msg = f'Follow-up on task "{task.name}"'
        ActivityLog.objects.create(complaint=complaint, message=msg[:300], actor_name=actor)
        notify_involved(complaint, user, f'{actor} - "{complaint.title}": {msg}')

    def perform_update(self, serializer):
        self._own(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._own(instance)
        instance.delete()


class FollowupViewSet(
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """PATCH /followups/<id>/ to edit a note, DELETE /followups/<id>/ to remove it."""

    serializer_class = FollowupSerializer

    def get_queryset(self):
        qs = visible_to(self.request.user, Followup.objects.all(), prefix="complaint__")
        return qs.prefetch_related("attachments", "reactions__user")

    @action(detail=True, methods=["post"])
    def react(self, request, pk=None):
        """Toggle an emoji on a follow-up: POST {"emoji": "thumbsup"}.
        Anyone who can see the case can react; sending the same emoji again removes it."""
        followup = self.get_object()  # 404 unless the case is visible to them
        key = str(request.data.get("emoji") or "")
        if key not in REACTION_KEYS:
            raise ValidationError({"detail": "That reaction is not available."})
        reaction, created = Reaction.objects.get_or_create(
            followup=followup, user=request.user, emoji=key
        )
        if not created:
            reaction.delete()
        fresh = self.get_queryset().get(pk=followup.pk)
        return Response(self.get_serializer(fresh).data)

    def perform_update(self, serializer):
        obj = serializer.save()
        c = obj.complaint
        notify_involved(
            c,
            self.request.user,
            f'{display_name(self.request.user)} edited a follow-up on "{c.title}"',
        )

    def perform_destroy(self, instance):
        c = instance.complaint
        instance.delete()
        notify_involved(
            c,
            self.request.user,
            f'{display_name(self.request.user)} deleted a follow-up on "{c.title}"',
        )


class AttachmentViewSet(mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """DELETE /attachments/<id>/ removes an image (its uploader or the admin only).
    The file is removed from R2 as well."""

    serializer_class = AttachmentSerializer

    def get_queryset(self):
        visible = visible_to(self.request.user, Complaint.objects.all()).values("pk")
        return Attachment.objects.filter(
            Q(complaint_id__in=visible) | Q(followup__complaint_id__in=visible)
        )

    def perform_destroy(self, instance):
        user = self.request.user
        if not user.is_superuser and instance.uploaded_by_id != user.id:
            raise PermissionDenied("Only the person who added this image can delete it.")
        instance.delete()


# ---------- notifications ----------

class NotificationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Only ever the logged-in user's own notifications."""

    serializer_class = NotificationSerializer
    pagination_class = None

    def get_queryset(self):
        user = self.request.user
        qs = Notification.objects.filter(recipient=user)
        if not user.is_superuser:  # never show a notice about work from the other side
            qs = qs.filter(
                Q(complaint__isnull=True) | Q(complaint__kind__in=allowed_kinds(user))
            )
        return qs

    def list(self, request, *args, **kwargs):
        run_sprint_maintenance()  # the app polls this, so sprints close on time without a cron job
        qs = self.get_queryset()[:30]
        return Response(self.get_serializer(qs, many=True).data)

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        n = self.get_object()
        n.is_read = True
        n.save(update_fields=["is_read"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["post"], url_path="read-all")
    def read_all(self, request):
        self.get_queryset().filter(is_read=False).update(is_read=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------- admin-managed lists ----------

class ConfigView(APIView):
    """Everything the screens need in one call: statuses, priorities, categories."""

    def get(self, request):
        seed_defaults()
        return Response(
            {
                "statuses": StatusSerializer(Status.objects.all(), many=True).data,
                "priorities": PrioritySerializer(Priority.objects.all(), many=True).data,
                "categories": CategorySerializer(Category.objects.all(), many=True).data,
            }
        )


class OptionViewSet(viewsets.ModelViewSet):
    """Everyone logged in can read the lists; only the admin can change them."""

    pagination_class = None
    noun = "item"

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsSuperAdmin()]

    def list(self, request, *args, **kwargs):
        seed_defaults()
        return super().list(request, *args, **kwargs)

    def tasks_using(self, instance):
        raise NotImplementedError

    def perform_destroy(self, instance):
        if self.get_queryset().count() <= 1:
            raise ValidationError({"detail": f"Keep at least one {self.noun}."})
        n = self.tasks_using(instance)
        if n:
            raise ValidationError(
                {
                    "detail": f"{n} task{'s' if n != 1 else ''} still use this {self.noun}. "
                    "Move them to another one first."
                }
            )
        instance.delete()

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        """POST {"ids": [3, 1, 2]} - saves the new order."""
        try:
            ids = [int(i) for i in request.data.get("ids", [])]
        except (TypeError, ValueError):
            raise ValidationError({"detail": "ids must be a list of numbers."})
        items = {o.id: o for o in self.get_queryset()}
        for position, pk in enumerate(ids):
            obj = items.get(pk)
            if obj and obj.order != position:
                obj.order = position
                obj.save(update_fields=["order"])
        return Response(self.get_serializer(self.get_queryset(), many=True).data)


class StatusViewSet(OptionViewSet):
    serializer_class = StatusSerializer
    queryset = Status.objects.all()
    noun = "status"

    def tasks_using(self, instance):
        return Complaint.objects.filter(status=instance.value).count()

    def perform_destroy(self, instance):
        if instance.value in PROTECTED_STATUSES:
            raise ValidationError(
                {"detail": f'"{instance.label}" is a built-in status and can\'t be deleted.'}
            )
        super().perform_destroy(instance)

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        """Same as the base reorder, but "Closed" always stays last."""
        super().reorder(request)
        ensure_core_statuses()
        return Response(self.get_serializer(self.get_queryset(), many=True).data)


class PriorityViewSet(OptionViewSet):
    serializer_class = PrioritySerializer
    queryset = Priority.objects.all()
    noun = "priority"

    def tasks_using(self, instance):
        return Complaint.objects.filter(priority=instance.value).count()


class CategoryViewSet(OptionViewSet):
    serializer_class = CategorySerializer
    queryset = Category.objects.all()
    noun = "category"

    def tasks_using(self, instance):
        return Complaint.objects.filter(category=instance.name).count()

    def perform_update(self, serializer):
        old = serializer.instance.name
        obj = serializer.save()
        if obj.name != old:  # keep existing tasks attached to the renamed category
            Complaint.objects.filter(category=old).update(category=obj.name)


# ---------- projects ----------

class ProjectViewSet(viewsets.ModelViewSet):
    """Admin manages projects and their members. Users only list the ones they are in."""

    serializer_class = ProjectSerializer
    pagination_class = None

    def get_permissions(self):
        if self.action in ("list", "retrieve", "history"):
            return [IsAuthenticated()]
        return [IsSuperAdmin()]

    def get_queryset(self):
        seed_defaults()
        user = self.request.user
        qs = Project.objects.prefetch_related("members", "sections__owners")
        if not user.is_superuser:
            qs = qs.filter(members__user=user)
        kinds = allowed_kinds(user)  # counts only cover the viewer's side
        return qs.annotate(
            task_count=Count("complaints", filter=Q(complaints__kind__in=kinds), distinct=True),
            open_count=Count(
                "complaints",
                filter=Q(complaints__kind__in=kinds)
                & ~Q(complaints__status__in=closed_status_values()),
                distinct=True,
            ),
        )

    def perform_create(self, serializer):
        project = serializer.save()
        actor = self.request.user
        members = list(project.members.all())
        log_project(project, "Project created", actor, kind="created")
        name = display_name(actor)
        ProjectActivity.objects.bulk_create(
            [
                ProjectActivity(
                    project=project,
                    message=f"{m.full_name} added to the project"[:300],
                    actor_name=name,
                )
                for m in members
            ]
        )
        notify_project_members(
            project,
            members,
            actor,
            f'{name} added you to the project "{project.name}"',
        )

    def perform_update(self, serializer):
        old_name = serializer.instance.name
        before = set(serializer.instance.members.values_list("id", flat=True))
        twin = serializer.instance.twin()  # looked up under the old name
        project = serializer.save()
        if twin and project.name != old_name:
            Project.objects.filter(pk=twin.pk).update(name=project.name)  # keep the pair together
            log_project(twin, f'Renamed from "{old_name}" to "{project.name}"', self.request.user)
        after = set(project.members.values_list("id", flat=True))
        actor = self.request.user
        name = display_name(actor)
        added = list(TeamMember.objects.filter(id__in=after - before))
        removed = list(TeamMember.objects.filter(id__in=before - after))
        for section in project.sections.all():
            section.owners.remove(*removed)  # they can no longer own work in this project

        entries = []
        if project.name != old_name:
            entries.append(f'Renamed from "{old_name}" to "{project.name}"')
        entries += [f"{m.full_name} added to the project" for m in added]
        entries += [f"{m.full_name} removed from the project" for m in removed]
        ProjectActivity.objects.bulk_create(
            [
                ProjectActivity(project=project, message=m[:300], actor_name=name)
                for m in entries
            ]
        )

        notify_project_members(
            project,
            added,
            actor,
            f'{name} added you to the project "{project.name}"',
        )
        notify_project_members(
            project,
            removed,
            actor,
            f'{name} removed you from the project "{project.name}"',
            link=False,
        )

    def perform_destroy(self, instance):
        # Admin can delete any project, including its tasks (and the last project).
        members = list(instance.members.all())
        name, actor = instance.name, self.request.user
        with transaction.atomic():
            instance.complaints.all().delete()  # tasks are PROTECTed, so remove them first
            instance.delete()
            Sprint.objects.filter(projects__isnull=True).delete()  # sprints left with no project
        notify_project_members(
            None,
            members,
            actor,
            f'{display_name(actor)} deleted the project "{name}"',
            link=False,
        )

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        """Newest first: project events, plus every task event inside the project
        (task created, every task change, follow-ups, moves and deletions)."""
        project = self.get_object()  # same visibility rules as the project list
        limit = 300
        kinds = allowed_kinds(request.user)

        entries = [
            {
                "id": f"project-{a.id}",
                "type": a.kind,
                "actor": a.actor_name,
                "message": a.message,
                "created_at": a.created_at,
            }
            for a in project.activity.filter(
                Q(task_kind="") | Q(task_kind__in=kinds)
            )[:limit]
        ]

        for c in Complaint.objects.filter(project=project, kind__in=kinds):
            entries.append(
                {
                    "id": f"task-{c.id}-created",
                    "type": "created",
                    "actor": c.created_by_name,
                    "message": f"Task {task_ref(c)} created",
                    "created_at": c.created_at,
                }
            )

        logs = (
            ActivityLog.objects.filter(complaint__project=project, complaint__kind__in=kinds)
            .select_related("complaint")
            .order_by("-created_at")[:limit]
        )
        for a in logs:
            entries.append(
                {
                    "id": f"log-{a.id}",
                    "type": "change",
                    "actor": a.actor_name,
                    "message": f"{task_ref(a.complaint)}: {a.message}",
                    "created_at": a.created_at,
                }
            )

        notes = (
            Followup.objects.filter(complaint__project=project, complaint__kind__in=kinds)
            .select_related("complaint")
            .order_by("-created_at")[:limit]
        )
        for f in notes:
            entries.append(
                {
                    "id": f"followup-{f.id}",
                    "type": "followup",
                    "actor": f.created_by_name,
                    "message": f"Follow-up on {task_ref(f.complaint)}: {f.note}",
                    "created_at": f.created_at,
                }
            )

        # Projects made before this log existed have no "created" line yet.
        if not project.activity.filter(kind="created").exists():
            entries.append(
                {
                    "id": "created",
                    "type": "created",
                    "actor": "",
                    "message": "Project created",
                    "created_at": project.created_at,
                }
            )

        entries.sort(key=lambda e: e["created_at"], reverse=True)
        return Response(entries[:limit])


# ---------- sections (working areas inside a development project) ----------

class SectionViewSet(viewsets.ModelViewSet):
    """The admin creates the working sections of a development project (for example
    "Shift Management"); developers add their tasks and bugs under them.

    GET /sections/?project=<id>
    """

    serializer_class = SectionSerializer
    pagination_class = None

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsSuperAdmin()]

    def get_queryset(self):
        seed_defaults()
        user = self.request.user
        qs = Section.objects.select_related("project").prefetch_related("owners")
        if not user.is_superuser:
            if user_access(user) == "support":
                return qs.none()  # sections belong to the development side
            qs = qs.filter(project__members__user=user)
        params = self.request.query_params
        if params.get("project", "").isdigit():
            qs = qs.filter(project_id=params["project"])
        return qs.annotate(
            task_count=Count(
                "complaints", filter=Q(complaints__kind__in=DEV_KINDS), distinct=True
            ),
            open_count=Count(
                "complaints",
                filter=Q(complaints__kind__in=DEV_KINDS)
                & ~Q(complaints__status__in=closed_status_values()),
                distinct=True,
            ),
        )

    def perform_create(self, serializer):
        user = self.request.user
        section = serializer.save(created_by_name=display_name(user))
        log_project(section.project, f'Section "{section.name}" added', user, task_kind="task")
        owners = list(TeamMember.objects.filter(owned_sections=section))
        if owners:
            names = ", ".join(m.full_name for m in owners)
            log_project(section.project, f'Section "{section.name}" owners: {names}', user, task_kind="task")
            notify_new_section_owners(section, owners, user)

    def perform_update(self, serializer):
        old_name = serializer.instance.name
        before = set(serializer.instance.owners.values_list("id", flat=True))
        section = serializer.save()
        if section.name != old_name:
            log_project(
                section.project,
                f'Section renamed from "{old_name}" to "{section.name}"',
                self.request.user,
                task_kind="task",
            )
        after = set(TeamMember.objects.filter(owned_sections=section).values_list("id", flat=True))
        added = list(TeamMember.objects.filter(id__in=after - before))
        removed = list(TeamMember.objects.filter(id__in=before - after))
        for m in added:
            log_project(section.project, f'{m.full_name} is now owner of "{section.name}"', self.request.user, task_kind="task")
        for m in removed:
            log_project(section.project, f'{m.full_name} is no longer owner of "{section.name}"', self.request.user, task_kind="task")
        notify_new_section_owners(section, added, self.request.user)

    def perform_destroy(self, instance):
        project, name = instance.project, instance.name
        n = instance.complaints.count()
        instance.delete()  # its tasks stay in the project, just without a section
        log_project(
            project,
            f'Section "{name}" deleted ({n} task{"s" if n != 1 else ""} left without a section)',
            self.request.user,
            task_kind="task",
        )


# ---------- sprints (development projects only) ----------

class IsDeveloperStaff(BasePermission):
    """The admin, or a login with developer (or both) access."""

    def has_permission(self, request, view):
        u = request.user
        return bool(
            u and u.is_authenticated and (u.is_superuser or user_access(u) in ("developer", "both"))
        )


def case_ids(request):
    try:
        ids = [int(i) for i in request.data.get("cases", [])]
    except (TypeError, ValueError):
        raise ValidationError({"detail": "cases must be a list of task ids."})
    if not ids:
        raise ValidationError({"detail": "Choose at least one task."})
    return ids


def project_people(sprint):
    """Developer-side members of every project in the sprint, each person once.
    This is who the admin can pick from; it is not who is in the sprint."""
    seen = {}
    for project in sprint.projects.all():
        for m in project_side_members(project, "task"):
            seen[m.id] = m
    return list(seen.values())


def sprint_members(sprint):
    """The people the admin put in this sprint (with an active login). Only they see
    the sprint and get its notices."""
    return list(
        sprint.members.filter(user__isnull=False, user__is_active=True).select_related("user")
    )


def eligible_sprint_members(sprint):
    """Team members who may be in the sprint: developers of any of its projects."""
    return TeamMember.objects.filter(
        projects__in=sprint.projects.all(), access__in=("developer", "both")
    ).distinct()


def log_sprint(sprint, message, actor):
    """One history line in every project that takes part in the sprint."""
    for project in sprint.projects.all():
        log_project(project, message, actor, task_kind="task")


def notify_sprint(sprint, actor, text, skip_ids=()):
    first = sprint.projects.first()
    members = [m for m in sprint_members(sprint) if m.user_id not in skip_ids]
    notify_project_members(first, members, actor, text)


def notify_new_section_owners(section, owners, actor):
    """Tell people they now own a section (and which sprint it is part of, if any)."""
    if not owners:
        return
    sprint = (
        Sprint.objects.filter(projects=section.project, status__in=("active", "planned"))
        .order_by("status", "start_date")  # "active" sorts before "planned"
        .first()
    )
    where = f' in sprint "{sprint.name}"' if sprint else ""
    notify_project_members(
        section.project,
        owners,
        actor,
        f'{display_name(actor)} made you owner of "{section.name}"{where}',
    )


def notify_section_owners_of_sprint(sprint, actor):
    """When a sprint starts, tell each section owner which sections are theirs.
    Returns the user ids notified (they skip the general "sprint started" notice)."""
    mine = {}  # user id -> (user, [section names])
    in_sprint = set(sprint.members.values_list("id", flat=True))  # owners outside the sprint hear nothing
    sections = Section.objects.filter(project__in=sprint.projects.all()).prefetch_related("owners__user")
    for section in sections:
        for m in section.owners.all():
            if m.id in in_sprint and m.user_id and m.user.is_active and m.user_id != actor.id:
                mine.setdefault(m.user_id, (m.user, []))[1].append(section.name)
    project = sprint.projects.first()
    for uid, (user, names) in mine.items():
        notify_users(
            [user],
            f'Sprint "{sprint.name}" started: you own {", ".join(names)}',
            project=project,
            actor_name=display_name(actor),
        )
    return set(mine)


SPRINT_WARN_DAYS = 2  # warn once a day when the end date is this close and work is still open


def admin_users(skip_user=None):
    return [
        u
        for u in User.objects.filter(is_superuser=True, is_active=True)
        if skip_user is None or u.id != skip_user.id
    ]


def notify_users(users, text, complaint=None, project=None, actor_name="System"):
    """One on-screen notice per user (used for warnings and automatic events)."""
    Notification.objects.bulk_create(
        [
            Notification(
                recipient=u,
                complaint=complaint,
                project=project,
                message=text[:300],
                actor_name=actor_name,
            )
            for u in users
        ]
    )


def task_timing(complaint):
    """'on time', '3 days late' or 'completed' for a task finished inside a sprint.
    The task's own due date wins; otherwise the sprint's end date is the deadline."""
    sprint = complaint.sprint
    deadline = complaint.due_date or (sprint.end_date if sprint else None)
    if deadline is None:
        return "completed"
    late = (timezone.localdate() - deadline).days
    if late <= 0:
        return "on time"
    return f"{late} day{'s' if late != 1 else ''} late"


def notify_task_done(complaint, actor):
    """Tell the admin(s) a sprint task was completed. Returns the user ids notified."""
    sprint = complaint.sprint
    admins = admin_users(skip_user=actor)
    notify_users(
        admins,
        f'{display_name(actor)} completed "{complaint.title}" in sprint "{sprint.name}" ({task_timing(complaint)})',
        complaint=complaint,
        actor_name=display_name(actor),
    )
    if not sprint.complaints.exclude(status__in=closed_status_values()).exists():
        tail = (
            f"It closes by itself after {sprint.end_date:%d %b}."
            if sprint.auto_close and sprint.end_date
            else "Complete it, or add more tasks."
        )
        notify_users(
            admins,
            f'Every task in sprint "{sprint.name}" is done. {tail}'[:300],
            project=sprint.projects.first(),
            actor_name=display_name(actor),
        )
    return {u.id for u in admins}


def task_people(complaint):
    """Users who work on a task: the assignee and the helpers (with an active login)."""
    users = {}
    if complaint.assigned_to and complaint.assigned_to.user_id and complaint.assigned_to.user.is_active:
        users[complaint.assigned_to.user_id] = complaint.assigned_to.user
    for m in complaint.helpers.select_related("user"):
        if m.user_id and m.user.is_active:
            users[m.user_id] = m.user
    return users


def warn_unfinished(sprint, unfinished, lead, tail, skip_ids=()):
    """Warn every person who still has open work in the sprint, plus the admin.
    Returns the user ids that got a personal warning."""
    project = sprint.projects.first()
    in_sprint = set(sprint.members.values_list("user_id", flat=True))  # only people in the sprint are warned
    per_user = {}
    for c in unfinished:
        for uid, u in task_people(c).items():
            if uid in in_sprint:
                per_user.setdefault(uid, (u, []))[1].append(c)
    for uid, (u, tasks) in per_user.items():
        if uid in skip_ids:
            continue
        first = tasks[0]
        more = f" and {len(tasks) - 1} more" if len(tasks) > 1 else ""
        notify_users(
            [u],
            f'Warning: sprint "{sprint.name}" {lead} and "{first.title}"{more} is not done. {tail}',
            complaint=first,
        )
    by_section = {}
    for c in unfinished:
        key = c.section.name if c.section_id else "No section"
        by_section[key] = by_section.get(key, 0) + 1
    sections = ", ".join(f"{name} ({n})" for name, n in by_section.items())
    notify_users(
        [u for u in admin_users() if u.id not in skip_ids],
        f'Warning: sprint "{sprint.name}" {lead} but is not completed. Not done: {sections}. {tail}',
        project=project,
    )
    return set(per_user)


def warn_sprint_closing(sprint, days):
    """The sprint is near (or past) its end date with open work. Once a day at most."""
    today = timezone.localdate()
    sprint.closing_warned_on = today
    sprint.save(update_fields=["closing_warned_on"])
    unfinished = list(
        sprint.complaints.exclude(status__in=closed_status_values())
        .select_related("assigned_to__user", "section")
    )
    if not unfinished:
        return
    if days > 0:
        lead = f"ends in {days} day{'s' if days != 1 else ''}"
    elif days == 0:
        lead = "ends today"
    else:
        lead = f"ended {-days} day{'s' if days != -1 else ''} ago"
    tail = (
        "It will close automatically and open tasks go back to the backlog."
        if sprint.auto_close
        else "The admin has to close it."
    )
    warn_unfinished(sprint, unfinished, lead, tail)


def close_sprint(sprint, actor=None, target=None):
    """Complete an active sprint. `actor` is the admin who did it, or None when the
    sprint closes by itself after its end date. Open tasks go to `target` (a planned
    sprint) or back to the backlog. Returns False if it was already closed."""
    closed = closed_status_values()
    actor_name = display_name(actor) if actor else "Auto-close"
    where = f'sprint "{target.name}"' if target else "the backlog"
    with transaction.atomic():
        sprint = Sprint.objects.select_for_update().get(pk=sprint.pk)
        if sprint.status != "active":
            return False
        unfinished = list(
            sprint.complaints.exclude(status__in=closed).select_related("assigned_to__user", "section")
        )
        left = [c.id for c in unfinished]
        Complaint.objects.filter(id__in=left).update(sprint=target)
        ActivityLog.objects.bulk_create(
            [
                ActivityLog(
                    complaint_id=cid,
                    message=f'Sprint "{sprint.name}" ended, moved to {where}'[:300],
                    actor_name=actor_name,
                )
                for cid in left
            ]
        )
        sprint.status = "completed"
        sprint.completed_at = timezone.now()
        if not sprint.end_date:
            sprint.end_date = timezone.localdate()
        sprint.completed_by_name = actor_name
        sprint.unfinished_at_close = len(left)
        sprint.save(
            update_fields=["status", "completed_at", "end_date", "completed_by_name", "unfinished_at_close"]
        )

    n = len(left)
    log_sprint(
        sprint,
        f'Sprint "{sprint.name}" {"completed" if actor else "closed automatically"} '
        f'({n} unfinished task{"s" if n != 1 else ""} moved to {where})',
        actor_name,
    )
    project = sprint.projects.first()
    members = sprint_members(sprint)
    skip = set()
    if n:
        skip = warn_unfinished(
            sprint,
            unfinished,
            "has closed",
            f"It is not completed. Moved to {where}.",
            skip_ids={actor.id} if actor else (),
        )
    if actor:
        notify_project_members(
            project, [m for m in members if m.user_id not in skip], actor,
            f'{display_name(actor)} completed sprint "{sprint.name}"',
        )
    else:
        rest = [m.user for m in members if m.user_id and m.user_id not in skip]
        text = (
            f'Sprint "{sprint.name}" closed automatically. All tasks were done.'
            if n == 0
            else f'Sprint "{sprint.name}" closed automatically on its end date. {n} task{"s" if n != 1 else ""} not done, moved to {where}.'
        )
        notify_users(rest, text, project=project)
        if n == 0:
            notify_users(admin_users(), text, project=project)
    return True


def run_sprint_maintenance():
    """Close sprints whose end date has passed and send the daily closing warnings.
    Cheap to call often. Runs whenever the notification bell polls, whenever the sprint
    list is opened, and from `manage.py close_sprints` if you also want a cron job."""
    today = timezone.localdate()
    for sprint in Sprint.objects.filter(status="active", end_date__isnull=False).prefetch_related("projects"):
        days = (sprint.end_date - today).days
        try:
            if days < 0 and sprint.auto_close:
                close_sprint(sprint)
            elif days <= SPRINT_WARN_DAYS and sprint.closing_warned_on != today:
                warn_sprint_closing(sprint, days)
        except Exception:  # one bad sprint must never break the screen that triggered this
            continue


class SprintViewSet(viewsets.ModelViewSet):
    """The admin plans sprints and adds development projects to them. Tasks and bugs
    of those projects (grouped by section) are then worked inside the sprint.

    GET  /sprints/?project=<id>&status=<planned|active|completed>
    POST /sprints/                     body {"name", "goal", "start_date", "end_date", "projects": [ids], "members": [ids]}
    POST /sprints/<id>/start/          planned -> active (one active sprint per project)
    POST /sprints/<id>/complete/       active -> completed, body {"move_to": <sprint id>|null}
                                       decides where unfinished tasks go (default: backlog)
    POST /sprints/<id>/add-cases/      body {"cases": [ids]}
    POST /sprints/<id>/remove-cases/   body {"cases": [ids]}
    Tasks of a sprint: GET /complaints/?sprint=<id>   (backlog: ?sprint=none)

    Admin only: create, edit, delete, start, complete. The admin picks who works in each
    sprint ("members": [team member ids], developers of the sprint's projects). Only
    those people see the sprint and get its notices; they can move their own project's
    tasks in and out.
    """

    serializer_class = SprintSerializer
    pagination_class = None

    def get_permissions(self):
        if self.action in ("list", "retrieve", "add_cases", "remove_cases"):
            return [IsDeveloperStaff()]
        return [IsSuperAdmin()]

    def get_queryset(self):
        seed_defaults()
        user = self.request.user
        qs = Sprint.objects.prefetch_related("projects__members", "members")
        if not user.is_superuser:
            # Only the people the admin put in the sprint (who are still in one of its projects).
            qs = qs.filter(members__user=user, projects__members__user=user)
        params = self.request.query_params
        if params.get("project", "").isdigit():
            qs = qs.filter(projects__id=params["project"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        return qs.annotate(
            total_count=Count("complaints", distinct=True),
            done_count=Count(
                "complaints",
                filter=Q(complaints__status__in=closed_status_values()),
                distinct=True,
            ),
        )

    def list(self, request, *args, **kwargs):
        run_sprint_maintenance()
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        run_sprint_maintenance()
        return super().retrieve(request, *args, **kwargs)

    def fresh(self, sprint):
        return self.get_serializer(self.get_queryset().get(pk=sprint.pk)).data

    def perform_create(self, serializer):
        user = self.request.user
        sprint = serializer.save(created_by_name=display_name(user))
        if "members" not in self.request.data:  # old clients: everyone in the projects, as before
            sprint.members.set([m.id for m in project_people(sprint)])
        names = ", ".join(p.name for p in sprint.projects.all())
        log_sprint(sprint, f'Sprint "{sprint.name}" created ({names})', user)
        notify_sprint(sprint, user, f'{display_name(user)} added you to sprint "{sprint.name}"')

    def perform_update(self, serializer):
        old_name = serializer.instance.name
        old_end = serializer.instance.end_date
        before = set(serializer.instance.projects.values_list("id", flat=True))
        people_before = set(serializer.instance.members.values_list("id", flat=True))
        sprint = serializer.save()
        if sprint.end_date != old_end and sprint.closing_warned_on:
            sprint.closing_warned_on = None  # new deadline: warn again when it gets close
            sprint.save(update_fields=["closing_warned_on"])
        after = set(sprint.projects.values_list("id", flat=True))
        user = self.request.user

        # Someone who is no longer in any of the sprint's projects can't stay in the sprint.
        stale = sprint.members.exclude(id__in=eligible_sprint_members(sprint).values("id"))
        if stale.exists():
            sprint.members.remove(*stale)
        people_after = set(sprint.members.values_list("id", flat=True))
        if people_after != people_before and sprint.status != "completed":
            added = TeamMember.objects.filter(id__in=people_after - people_before).select_related("user")
            left = TeamMember.objects.filter(id__in=people_before - people_after).select_related("user")
            project = sprint.projects.first()
            name = display_name(user)
            notify_project_members(project, added, user, f'{name} added you to sprint "{sprint.name}"')
            notify_project_members(
                project, left, user, f'{name} removed you from sprint "{sprint.name}"', link=False
            )
            bits = [f"+{m.full_name}" for m in added] + [f"-{m.full_name}" for m in left]
            log_sprint(sprint, f'People in sprint "{sprint.name}" changed: {", ".join(bits)}', user)

        if sprint.name != old_name:
            log_sprint(sprint, f'Sprint renamed from "{old_name}" to "{sprint.name}"', user)

        for project in Project.objects.filter(id__in=after - before):
            log_project(project, f'Added to sprint "{sprint.name}"', user, task_kind="task")
        for project in Project.objects.filter(id__in=before - after):
            # A project that leaves the sprint takes its tasks back to the backlog.
            moved = Complaint.objects.filter(sprint=sprint, project=project)
            n = moved.count()
            moved.update(sprint=None)
            log_project(
                project,
                f'Removed from sprint "{sprint.name}" ({n} task{"s" if n != 1 else ""} back to backlog)',
                user,
                task_kind="task",
            )

    def perform_destroy(self, instance):
        name = instance.name
        projects = list(instance.projects.all())
        n = instance.complaints.count()
        instance.delete()  # its cases go back to the backlog
        for project in projects:
            log_project(
                project,
                f'Sprint "{name}" deleted ({n} task{"s" if n != 1 else ""} back to backlog)',
                self.request.user,
                task_kind="task",
            )

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        sprint = self.get_object()
        if sprint.status != "planned":
            raise ValidationError({"detail": "Only a planned sprint can be started."})
        if not sprint.members.exists():
            raise ValidationError({"detail": "Choose who works in this sprint before starting it."})
        busy = (
            Project.objects.filter(sprints__status="active", id__in=sprint.projects.all())
            .distinct()
            .values_list("name", flat=True)
        )
        if busy:
            raise ValidationError(
                {
                    "detail": f"{', '.join(busy)} already "
                    f"{'has' if len(busy) == 1 else 'have'} an active sprint. Complete it first."
                }
            )
        sprint.status = "active"
        if not sprint.start_date:
            sprint.start_date = timezone.localdate()
        sprint.save(update_fields=["status", "start_date"])

        actor = display_name(request.user)
        log_sprint(sprint, f'Sprint "{sprint.name}" started', request.user)
        owner_ids = notify_section_owners_of_sprint(sprint, request.user)
        notify_sprint(sprint, request.user, f'{actor} started sprint "{sprint.name}"', skip_ids=owner_ids)
        return Response(self.fresh(sprint))

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        sprint = self.get_object()
        if sprint.status != "active":
            raise ValidationError({"detail": "Only an active sprint can be completed."})

        unfinished = sprint.complaints.exclude(status__in=closed_status_values())

        target = None
        move_to = request.data.get("move_to")
        if move_to not in (None, "", "backlog"):
            try:
                target = Sprint.objects.get(id=int(move_to), status="planned")
            except (TypeError, ValueError, Sprint.DoesNotExist):
                raise ValidationError({"detail": "Unfinished tasks can only move to a planned sprint."})
            needed = set(unfinished.values_list("project_id", flat=True))
            have = set(target.projects.values_list("id", flat=True))
            if not needed <= have:
                raise ValidationError(
                    {"detail": "That sprint doesn't include every project that still has unfinished tasks."}
                )

        if not close_sprint(sprint, actor=request.user, target=target):
            raise ValidationError({"detail": "Only an active sprint can be completed."})
        return Response(self.fresh(sprint))

    @action(detail=True, methods=["post"], url_path="add-cases")
    def add_cases(self, request, pk=None):
        sprint = self.get_object()
        if sprint.status == "completed":
            raise ValidationError({"detail": "This sprint is completed."})
        found = list(
            visible_to(
                request.user,
                Complaint.objects.filter(
                    id__in=case_ids(request),
                    kind__in=DEV_KINDS,
                    project__in=sprint.projects.all(),
                ),
            ).values_list("id", flat=True)
        )
        if not found:
            raise ValidationError({"detail": "No matching tasks in this sprint's projects."})
        actor = display_name(request.user)
        Complaint.objects.filter(id__in=found).update(sprint=sprint)
        ActivityLog.objects.bulk_create(
            [
                ActivityLog(
                    complaint_id=cid,
                    message=f'Added to sprint "{sprint.name}" by {actor}'[:300],
                    actor_name=actor,
                )
                for cid in found
            ]
        )
        return Response(self.fresh(sprint))

    @action(detail=True, methods=["post"], url_path="remove-cases")
    def remove_cases(self, request, pk=None):
        sprint = self.get_object()
        if sprint.status == "completed":
            raise ValidationError({"detail": "This sprint is completed."})
        found = list(
            visible_to(
                request.user,
                Complaint.objects.filter(id__in=case_ids(request), sprint=sprint),
            ).values_list("id", flat=True)
        )
        actor = display_name(request.user)
        Complaint.objects.filter(id__in=found).update(sprint=None)
        ActivityLog.objects.bulk_create(
            [
                ActivityLog(
                    complaint_id=cid,
                    message=f'Removed from sprint "{sprint.name}" by {actor}'[:300],
                    actor_name=actor,
                )
                for cid in found
            ]
        )
        return Response(self.fresh(sprint))