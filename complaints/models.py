import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models
from django.db.models.signals import m2m_changed, post_delete, pre_delete
from django.utils import timezone
from django.dispatch import receiver


ACCESS_CHOICES = [
    ("support", "Support"),      # complaints only
    ("developer", "Developer"),  # projects, tasks and bugs only
    ("both", "Both"),
]

KIND_CHOICES = [
    ("complaint", "Complaint"),  # support side, never inside a project
    ("task", "Task"),            # development side, always inside a project
    ("bug", "Bug"),
]


PRODUCT_TYPE_CHOICES = [
    ("own", "Own Product"),
    ("custom", "Custom Project"),
]


PROJECT_KIND_CHOICES = [
    ("development", "Development"),  # holds tasks and bugs; developers are members
    ("support", "Support"),          # holds complaints; support staff are members
]


TASK_ESCALATION_CHOICES = [
    ("admin", "Admin"),
    ("developers", "Developer team"),
]


SPRINT_STATUS_CHOICES = [
    ("planned", "Planned"),
    ("active", "Active"),
    ("completed", "Completed"),
]


class TeamMember(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    access = models.CharField(max_length=10, choices=ACCESS_CHOICES, default="developer")
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="team_member",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["first_name", "last_name"]

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.full_name


@receiver(pre_delete, sender=settings.AUTH_USER_MODEL)
def remove_team_member_with_login(sender, instance, **kwargs):
    """Deleting a login also removes its team member, so the person disappears from
    the team list and every project / assignee picker. Their tasks become unassigned."""
    TeamMember.objects.filter(user_id=instance.pk).delete()


class Complaint(models.Model):
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="task")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    customer = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    category = models.CharField(max_length=100, default="General")
    # Support cases only: which product the customer is reporting about.
    product_type = models.CharField(
        max_length=10, choices=PRODUCT_TYPE_CHOICES, default="own"
    )
    product_name = models.CharField(max_length=150, blank=True)
    # Support cases only: who the case was escalated to (they are also added as a helper).
    escalated_to = models.ForeignKey(
        TeamMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="escalated_complaints",
    )
    priority = models.CharField(max_length=20, default="medium")
    status = models.CharField(max_length=30, default="open")
    assigned_to = models.ForeignKey(
        TeamMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,  # removing a member unassigns their tasks
        related_name="complaints",
    )
    helpers = models.ManyToManyField(
        TeamMember, blank=True, related_name="helping"
    )  # extra people added for help; they can see and work on the task
    due_date = models.DateField(null=True, blank=True)
    project = models.ForeignKey(
        "Project",
        null=True,  # old tasks are moved into the first project automatically
        blank=True,
        on_delete=models.PROTECT,
        related_name="complaints",
    )
    # Tasks and bugs only: the working section of the project this task sits under
    # (e.g. "Shift Management"). The admin creates the sections; users add tasks in them.
    section = models.ForeignKey(
        "Section",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,  # deleting a section keeps its tasks, just unsectioned
        related_name="complaints",
    )
    # Tasks and bugs only: the sprint this task is planned or worked in (empty = backlog).
    sprint = models.ForeignKey(
        "Sprint",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,  # deleting a sprint sends its cases back to the backlog
        related_name="complaints",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="complaints_created",
    )
    created_by_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # When the task reached a "done" status (Status.is_closed). Cleared if it is reopened.
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        done = Status.objects.filter(value=self.status, is_closed=True).exists()
        before = self.closed_at
        if done and self.closed_at is None:
            self.closed_at = timezone.now()
        elif not done and self.closed_at is not None:
            self.closed_at = None
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and self.closed_at != before and "closed_at" not in update_fields:
            kwargs["update_fields"] = [*update_fields, "closed_at"]
        super().save(*args, **kwargs)

    @property
    def case_number(self):
        """Support case ID, e.g. OP-2026-0007 (own product) or CP-2026-0007 (custom
        project). Built from the permanent row id, so it never changes or repeats."""
        if self.kind != "complaint" or self.id is None:
            return None
        prefix = "OP" if self.product_type == "own" else "CP"
        year = self.created_at.year if self.created_at else 0
        return f"{prefix}-{year}-{self.id:04d}"

    def __str__(self):
        return self.title


class Followup(models.Model):
    complaint = models.ForeignKey(
        Complaint, on_delete=models.CASCADE, related_name="followups"
    )
    note = models.TextField()
    created_by_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Followup on complaint {self.complaint_id}"


# Reactions are stored as short keys (not the emoji characters themselves), so they
# are safe on any database encoding. The screen turns each key into its emoji.
REACTION_KEYS = [
    "thumbsup",
    "thumbsdown",
    "check",
    "smile",
    "heart",
    "tada",
    "eyes",
    "pray",
    "fire",
    "wow",
]


class Reaction(models.Model):
    """An emoji reaction on a follow-up. One per person per emoji; reacting again
    with the same emoji takes it back."""

    followup = models.ForeignKey(
        Followup, on_delete=models.CASCADE, related_name="reactions"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    emoji = models.CharField(max_length=20)  # one of REACTION_KEYS
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        unique_together = [("followup", "user", "emoji")]

    def __str__(self):
        return f"{self.user_id} reacted {self.emoji} on followup {self.followup_id}"


class Attachment(models.Model):
    """An image added to a follow-up, or to the case itself when it was filed.
    The file itself lives in Cloudflare R2."""

    followup = models.ForeignKey(
        Followup,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    # Images added when the case is first filed belong to the case (followup stays empty).
    complaint = models.ForeignKey(
        Complaint,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    key = models.CharField(max_length=500)  # path inside the R2 bucket
    url = models.URLField(max_length=700)
    name = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        if self.followup_id:
            return f"Image on followup {self.followup_id}"
        return f"Image on complaint {self.complaint_id}"


# ---------- Cloudflare R2 (S3-compatible) ----------
# Credentials are read from environment variables only (CLOUDFLARE_R2_*).

_r2_client = None


def r2_enabled():
    return os.environ.get("CLOUDFLARE_R2_ENABLED", "").lower() in ("1", "true", "yes")


def _r2():
    global _r2_client
    if _r2_client is None:
        import boto3
        from botocore.config import Config

        _r2_client = boto3.client(
            "s3",
            endpoint_url=os.environ["CLOUDFLARE_R2_BUCKET_ENDPOINT"],
            aws_access_key_id=os.environ["CLOUDFLARE_R2_ACCESS_KEY"],
            aws_secret_access_key=os.environ["CLOUDFLARE_R2_SECRET_KEY"],
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    return _r2_client


def r2_upload(fileobj, key, content_type):
    """Store the file and return its public URL."""
    _r2().upload_fileobj(
        fileobj,
        os.environ["CLOUDFLARE_R2_BUCKET"],
        key,
        ExtraArgs={"ContentType": content_type, "CacheControl": "public, max-age=31536000"},
    )
    return os.environ["CLOUDFLARE_R2_PUBLIC_URL"].rstrip("/") + "/" + key


def r2_delete(key):
    _r2().delete_object(Bucket=os.environ["CLOUDFLARE_R2_BUCKET"], Key=key)


@receiver(post_delete, sender=Attachment)
def remove_attachment_file(sender, instance, **kwargs):
    # Also runs when a follow-up or a whole task is deleted, so no file is left behind.
    try:
        r2_delete(instance.key)
    except Exception:
        pass


class ActivityLog(models.Model):
    """One line of history: who changed what on a complaint."""

    complaint = models.ForeignKey(
        Complaint, on_delete=models.CASCADE, related_name="activity"
    )
    message = models.CharField(max_length=300)
    actor_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.complaint_id}: {self.message}"

class ComplaintSeen(models.Model):
    """Records that a user has opened a task, so its red "new" light goes out for them."""

    complaint = models.ForeignKey(
        Complaint, on_delete=models.CASCADE, related_name="seen_by"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="seen_complaints"
    )
    seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("complaint", "user")]

    def __str__(self):
        return f"{self.user_id} saw {self.complaint_id}"


class ComplaintTask(models.Model):
    """A piece of work a support person adds to a complaint they are handling.
    Each has a name and a goal, and moves through the same statuses as everything
    else. The complaint's progress ring is the share of its tasks that are done."""

    complaint = models.ForeignKey(
        Complaint, on_delete=models.CASCADE, related_name="tasks"
    )
    name = models.CharField(max_length=200)
    goal = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=30, default="open")
    # Escalation: the support person hands this task up to the admin, or to the
    # developers of the same project ("" = not escalated).
    escalation = models.CharField(
        max_length=10, choices=TASK_ESCALATION_CHOICES, blank=True, default=""
    )
    escalated_at = models.DateTimeField(null=True, blank=True)
    escalated_by_name = models.CharField(max_length=150, blank=True)
    escalation_note = models.CharField(max_length=500, blank=True)
    # When escalated to developers: the task / bug created for them in the paired
    # development project. Its status is shown back on this support task.
    dev_task = models.ForeignKey(
        "Complaint",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,  # developers deleting their task leaves this one intact
        related_name="escalated_from_tasks",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_by_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    def save(self, *args, **kwargs):
        done = Status.objects.filter(value=self.status, is_closed=True).exists()
        before = self.closed_at
        if done and self.closed_at is None:
            self.closed_at = timezone.now()
        elif not done and self.closed_at is not None:
            self.closed_at = None
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and self.closed_at != before and "closed_at" not in update_fields:
            kwargs["update_fields"] = [*update_fields, "closed_at"]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class TaskFollowup(models.Model):
    """A note on one task of a support complaint (each task has its own thread)."""

    task = models.ForeignKey(
        ComplaintTask, on_delete=models.CASCADE, related_name="followups"
    )
    note = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_by_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"Followup on task {self.task_id}"


class Notification(models.Model):
    """Shown on screen to the person a task was assigned to."""

    NOTIFICATION_TYPES = [
        ("project_added", "Added to a project"),
        ("sprint_added", "Added to a sprint"),
    ]

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    complaint = models.ForeignKey(
        Complaint,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    project = models.ForeignKey(
        "Project",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notifications",
    )  # set for project notices (added to a project, etc.)
    # Set for "added to a sprint" notices, so the screen can show which project(s) it is in.
    sprint = models.ForeignKey(
        "Sprint",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    # "project_added" / "sprint_added" are shown as a sticky splash card on the side of the
    # screen until the user closes them. Empty for every other kind of notice.
    type = models.CharField(
        max_length=20, choices=NOTIFICATION_TYPES, blank=True, default=""
    )
    message = models.CharField(max_length=300)
    actor_name = models.CharField(max_length=150, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"To {self.recipient_id}: {self.message}"


# ---------- Admin-managed lists (statuses, priorities, categories) ----------

TONES = [
    ("grey", "Grey"),
    ("blue", "Blue"),
    ("yellow", "Yellow"),
    ("green", "Green"),
    ("purple", "Purple"),
    ("red", "Red"),
]


class Status(models.Model):
    """A board column. `value` is permanent (tasks store it); `label` can be renamed."""

    value = models.SlugField(max_length=30, unique=True)
    label = models.CharField(max_length=40)
    tone = models.CharField(max_length=10, choices=TONES, default="grey")
    is_closed = models.BooleanField(
        default=False, help_text="Tasks in this status no longer count as open work."
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        verbose_name_plural = "statuses"

    def __str__(self):
        return self.label


class Priority(models.Model):
    """Ordered lowest to highest."""

    value = models.SlugField(max_length=20, unique=True)
    label = models.CharField(max_length=40)
    tone = models.CharField(max_length=10, choices=TONES, default="grey")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        verbose_name_plural = "priorities"

    def __str__(self):
        return self.label


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Project(models.Model):
    """Tasks live inside a project. Non-admin users only see projects they belong to."""

    # A support project and a development project may share a name. Same name = the
    # two sides of one real project ("twins"), which is what lets support escalate a
    # task to the developers of that project. Names stay unique within one side.
    name = models.CharField(max_length=150)
    kind = models.CharField(
        max_length=15, choices=PROJECT_KIND_CHOICES, default="development"
    )
    members = models.ManyToManyField(TeamMember, blank=True, related_name="projects")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("name", "kind")]

    def twin(self):
        """The same-named project on the other side (support <-> development), or None."""
        other = "development" if self.kind == "support" else "support"
        return Project.objects.filter(name__iexact=self.name, kind=other).first()

    def __str__(self):
        return self.name


class Section(models.Model):
    """A working area inside a development project, e.g. "Shift Management".
    The admin creates sections; developers add their tasks and bugs under them."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=150)
    order = models.PositiveIntegerField(default=0)
    # The people responsible for this section. Optional. They are told when a sprint
    # starts and when tasks are added here, and see the section marked "Yours".
    owners = models.ManyToManyField(TeamMember, blank=True, related_name="owned_sections")
    created_by_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["project_id", "order", "id"]
        unique_together = [("project", "name")]

    def __str__(self):
        return self.name


class Sprint(models.Model):
    """A time-boxed batch of work. The admin plans a sprint and adds development
    projects to it; the tasks and bugs of those projects (grouped by section) are
    then worked inside it. Only one sprint per project can be active at a time."""

    projects = models.ManyToManyField(Project, blank=True, related_name="sprints")
    # The people who work in this sprint. Only they (and the admin) see the sprint and
    # get its notices (started, completed, closing warnings). Chosen by the admin from
    # the developers of the sprint's projects.
    members = models.ManyToManyField(TeamMember, blank=True, related_name="sprints")
    # Legacy: sprints used to belong to a single project. Kept only so old rows can be
    # copied into `projects` (see seed_sprint_projects); new code never reads it.
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    name = models.CharField(max_length=150)
    goal = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=SPRINT_STATUS_CHOICES, default="planned")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    created_by_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Complete by itself once the end date has passed. The admin can switch this off
    # for a sprint, and can always complete or extend a sprint by hand.
    auto_close = models.BooleanField(default=True)
    closing_warned_on = models.DateField(null=True, blank=True)  # last day a closing warning went out
    completed_by_name = models.CharField(max_length=150, blank=True)  # the admin, or "Auto-close"
    unfinished_at_close = models.PositiveIntegerField(default=0)  # tasks still open when it closed

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class ProjectActivity(models.Model):
    """One line of project history: project changes, and tasks moved or deleted.
    Task edits and follow-ups are read live from the task logs, not copied here."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="activity"
    )
    kind = models.CharField(max_length=20, default="change")  # "change" or "created"
    # Type of the task this line is about ("" for plain project events), so each
    # side only ever sees history about its own kind of work.
    task_kind = models.CharField(max_length=10, blank=True, default="")
    message = models.CharField(max_length=300)
    actor_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "project activity"

    def __str__(self):
        return f"{self.project_id}: {self.message}"


DEFAULT_STATUSES = [
    ("open", "Open", "grey", False),
    ("in_progress", "In progress", "blue", False),
    ("resolved", "Resolved", "green", True),
    ("closed", "Closed", "purple", True),
]
# "Open" and "Closed" are built in: they can't be deleted, "Closed" always counts as
# done, and "Closed" is always the LAST status (new statuses are slotted in before it).
CORE_OPEN = "open"
CORE_CLOSED = "closed"
PROTECTED_STATUSES = (CORE_OPEN, CORE_CLOSED)

DEFAULT_PRIORITIES = [
    ("low", "Low", "green"),
    ("medium", "Medium", "yellow"),
    ("high", "High", "red"),
]
DEFAULT_CATEGORIES = ["General", "Maintenance", "Service", "Billing"]


def ensure_core_statuses():
    """Self-healing rules for the status list. Safe to call any time:
    - "open" and "closed" always exist (recreated if someone removed them)
    - "closed" is always flagged as done, "open" never is
    - "closed" is always the last status
    """
    if not Status.objects.exists():
        return  # seed_defaults() will create the whole default set
    open_s, _ = Status.objects.get_or_create(
        value=CORE_OPEN, defaults={"label": "Open", "tone": "grey", "is_closed": False}
    )
    closed_s, _ = Status.objects.get_or_create(
        value=CORE_CLOSED, defaults={"label": "Closed", "tone": "purple", "is_closed": True}
    )
    if open_s.is_closed:
        open_s.is_closed = False
        open_s.save(update_fields=["is_closed"])
    if not closed_s.is_closed:
        closed_s.is_closed = True
        closed_s.save(update_fields=["is_closed"])
    ordered = list(Status.objects.exclude(pk=closed_s.pk)) + [closed_s]
    for position, s in enumerate(ordered):
        if s.order != position:
            s.order = position
            s.save(update_fields=["order"])


def seed_defaults():
    """Fill any empty list with the original defaults (runs once, on first use)."""
    if not Status.objects.exists():
        for i, (value, label, tone, closed) in enumerate(DEFAULT_STATUSES):
            Status.objects.create(
                value=value, label=label, tone=tone, is_closed=closed, order=i
            )
    ensure_core_statuses()
    if not Priority.objects.exists():
        for i, (value, label, tone) in enumerate(DEFAULT_PRIORITIES):
            Priority.objects.create(value=value, label=label, tone=tone, order=i)
    if not Category.objects.exists():
        for i, name in enumerate(DEFAULT_CATEGORIES):
            Category.objects.create(name=name, order=i)

    seed_sprint_projects()
    seed_default_project()


def seed_sprint_projects():
    """Old sprints belonged to one project. Copy that project into the new `projects`
    list, once, so existing sprints keep working after the upgrade."""
    for sprint in Sprint.objects.filter(project__isnull=False, projects__isnull=True):
        sprint.projects.add(sprint.project)


def seed_default_project():
    """First run: create a "General" project holding every existing task and
    team member, so nothing disappears when projects are introduced."""
    orphans = Complaint.objects.filter(project__isnull=True)
    first = Project.objects.order_by("id").first()
    if not orphans.exists():
        return first  # nothing to rescue; never bring back a project the admin deleted
    if first is None:
        first = Project.objects.create(name="General")
        first.members.set(TeamMember.objects.all())
    orphans.update(project=first)
    return first


# ---------- Passwords the admin set (so the admin can look them up later) ----------

class UserPassword(models.Model):
    """The password the admin typed for a login, kept encrypted.

    The real login password is still hashed by Django. This is only a readable
    copy for the admin screen, and it exists only for passwords the admin set.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="stored_password",
    )
    secret = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Stored password for user {self.user_id}"


def _fernet():
    # Optional: set PASSWORD_VAULT_KEY in settings to use a key separate from SECRET_KEY.
    material = getattr(settings, "PASSWORD_VAULT_KEY", None) or settings.SECRET_KEY
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest())
    return Fernet(key)


def save_stored_password(user, raw_password):
    token = _fernet().encrypt(raw_password.encode()).decode()
    UserPassword.objects.update_or_create(user=user, defaults={"secret": token})


def get_stored_password(user):
    """The readable password, or None if none was saved (or the key changed)."""
    row = UserPassword.objects.filter(user=user).first()
    if row is None:
        return None
    try:
        return _fernet().decrypt(row.secret.encode()).decode()
    except InvalidToken:
        return None


# ---------- "You were added to a project / sprint" notices ----------
# When the admin adds someone to a project or a sprint, that person gets a notice.
# The screen shows it as a sticky splash card until they close it (see NotificationBell).

def _notify_added(members, objs, kind):
    """One unread notice per login per project / sprint. Team members without a
    login (member.user empty) are skipped."""
    for m in members:
        if not m.user_id:
            continue
        for obj in objs:
            if kind == "project_added":
                Notification.objects.create(
                    recipient_id=m.user_id,
                    type=kind,
                    project=obj,
                    message=f"You were added to the project “{obj.name}”.",
                )
            else:
                Notification.objects.create(
                    recipient_id=m.user_id,
                    type=kind,
                    sprint=obj,
                    message=f"You were added to the sprint “{obj.name}”.",
                )


def _added_handler(kind, model):
    def handler(sender, instance, action, reverse, pk_set, **kwargs):
        if action != "post_add" or not pk_set:  # only people newly added, never removals
            return
        if reverse:  # member.projects.add(...) / member.sprints.add(...)
            _notify_added([instance], model.objects.filter(pk__in=pk_set), kind)
        else:  # project.members.add(...) / sprint.members.add(...)
            _notify_added(TeamMember.objects.filter(pk__in=pk_set), [instance], kind)

    return handler


m2m_changed.connect(
    _added_handler("project_added", Project),
    sender=Project.members.through,
    dispatch_uid="notify_project_added",
)
m2m_changed.connect(
    _added_handler("sprint_added", Sprint),
    sender=Sprint.members.through,
    dispatch_uid="notify_sprint_added",
)