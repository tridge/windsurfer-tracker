import SwiftUI
import WidgetKit

@main
struct TrackerWidgetBundle: WidgetBundle {
    var body: some Widget {
        if #available(iOS 16.2, *) {
            TrackerLiveActivity()
        }
    }
}
