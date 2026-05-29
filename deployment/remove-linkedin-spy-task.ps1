# remove-linkedin-spy-task.ps1
# Removes the Cora LinkedIn Spy scheduled task.

$TaskName = "Cora - LinkedIn Spy"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task: $TaskName" -ForegroundColor Yellow
} else {
    Write-Host "Task not found: $TaskName"
}
