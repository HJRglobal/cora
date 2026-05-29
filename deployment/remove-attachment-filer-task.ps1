# remove-attachment-filer-task.ps1
# Removes the Cora Email Attachment Filer scheduled task.

$TaskName = "Cora - Email Attachment Filer"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task: $TaskName" -ForegroundColor Yellow
} else {
    Write-Host "Task not found: $TaskName"
}
